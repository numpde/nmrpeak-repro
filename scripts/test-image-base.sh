#!/usr/bin/env bash
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 2; }

[[ $# -eq 2 ]] || fail "usage: test-image-base.sh <repository-root> <network-interface>"
readonly repo_root="$1"
readonly network_interface="$2"
[[ "$network_interface" == system || ( "$network_interface" =~ ^[A-Za-z0-9_.:-]{1,15}$ && "$network_interface" != . && "$network_interface" != .. && -d "/sys/class/net/$network_interface" ) ]] ||
    fail "selected network interface does not exist: $network_interface"
[[ "$(id -u)" -ne 0 && "$(id -g)" -ne 0 ]] ||
    fail "test image preparation requires a non-root user and primary group"

source "$repo_root/containers/test/base.env"
readonly python_digest="$PYTHON_BASE_DIGEST"
readonly python_ref="docker.io/library/$PYTHON_BASE_NAME@$python_digest"
readonly base_key="$("$repo_root/scripts/test-image-key.sh" "$repo_root")"
readonly test_image="nmrpeak-repro/python-test:$base_key"

[[ "${NMRPEAK_DOCKER_HOST:-}" =~ ^unix:///[^[:space:]]+$ ]] || fail 'Run test image preparation through make with a selected local Unix-socket context.'

existing_id="$(docker --host "$NMRPEAK_DOCKER_HOST" image ls --quiet --no-trunc "$test_image")"
if [[ -n "$existing_id" ]]; then
    existing_key="$(docker --host "$NMRPEAK_DOCKER_HOST" image inspect "$existing_id" \
        --format '{{index .Config.Labels "io.numpde.nmrpeak.test-base-key"}}')"
    [[ "$existing_key" == "$base_key" ]] || fail "existing test image has the wrong dependency key"
    printf '%s\n' "$test_image"
    exit 0
fi

readonly scratch="$(mktemp -d)"
readonly python_alias="localhost/nmrpeak-test/python-base:${python_digest#sha256:}-${scratch##*/}"
proxy_pid=""
engine_active=0
docker_base_imported=0
podman_alias_created=0
cleanup() {
    status=$?
    local retirement_error=""
    trap - EXIT
    if [[ -n "$proxy_pid" ]] && (( ! engine_active )); then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    if (( !engine_active && docker_base_imported )); then
        if docker_alias_id="$(docker --host "$NMRPEAK_DOCKER_HOST" image inspect "$python_alias" --format '{{.Id}}' 2>/dev/null)" \
            && [[ "$docker_alias_id" == "$docker_base_id" ]]; then
            docker --host "$NMRPEAK_DOCKER_HOST" image rm "$python_alias" >/dev/null ||
                retirement_error="selected-engine alias $python_alias could not be removed"
        else
            retirement_error="selected-engine alias $python_alias no longer identifies the imported base"
        fi
    fi
    if (( !engine_active && podman_alias_created )); then
        if podman_alias_id="$(podman --remote=false image inspect "$python_alias" --format '{{.Id}}' 2>/dev/null)" \
            && [[ "$podman_alias_id" == "$podman_base_id" ]]; then
            podman --remote=false image rm "$python_alias" >/dev/null ||
                retirement_error="${retirement_error:+$retirement_error; }Podman alias $python_alias could not be removed"
        else
            retirement_error="${retirement_error:+$retirement_error; }Podman alias $python_alias no longer identifies the pinned base"
        fi
    fi
    if (( engine_active )) || [[ -n "$retirement_error" ]]; then
        printf 'Build retirement is unconfirmed; preserve inputs at %s and proxy PID %s; the proxy may remain accessible to local processes. Inspect Docker and the process before cleanup or retrying.\n' "$scratch" "${proxy_pid:-none}" >&2
        [[ -z "$retirement_error" ]] || printf 'Base alias cleanup is unconfirmed: %s.\n' "$retirement_error" >&2
        (( status != 0 )) || status=1
    else
        if ! rm -rf -- "$scratch"; then
            printf 'Cannot confirm removal of base-image build inputs at %s.\n' "$scratch" >&2
            (( status != 0 )) || status=1
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

build_network=default
proxy_build_arguments=(--build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= --build-arg http_proxy= --build-arg https_proxy= --build-arg NO_PROXY= --build-arg no_proxy= --build-arg ALL_PROXY= --build-arg all_proxy= --build-arg FTP_PROXY= --build-arg ftp_proxy=)
if [[ "$network_interface" != system ]]; then
    setsid "${PYTHON:-python3}" "$repo_root/docker/bound_http_proxy.py" \
        --interface="$network_interface" --ready-file "$scratch/proxy.port" </dev/null >/dev/null 2>&1 &
    proxy_pid=$!
    for _ in {1..100}; do
        [[ -s "$scratch/proxy.port" ]] && break
        kill -0 "$proxy_pid" 2>/dev/null || fail "Network proxy stopped before test image preparation"
        sleep 0.05
    done
    [[ -s "$scratch/proxy.port" ]] || fail "Network proxy did not become ready"
    readonly proxy_url="http://127.0.0.1:$(<"$scratch/proxy.port")"
    build_network=host
    proxy_build_arguments=(--build-arg "HTTP_PROXY=$proxy_url" --build-arg "HTTPS_PROXY=$proxy_url"
        --build-arg "http_proxy=$proxy_url" --build-arg "https_proxy=$proxy_url"
        --build-arg "NO_PROXY=" --build-arg "no_proxy=" --build-arg "ALL_PROXY=" --build-arg "all_proxy=" --build-arg "FTP_PROXY=" --build-arg "ftp_proxy=")
fi

docker_base_id=""
docker_base_listing="$(docker --host "$NMRPEAK_DOCKER_HOST" image ls --digests --no-trunc \
    --format '{{.ID}} {{.Repository}}@{{.Digest}}' python)"
while read -r candidate_id candidate_reference extra; do
    [[ -n "${candidate_id:-}" ]] || continue
    [[ -z "${extra:-}" && "$candidate_id" =~ ^sha256:[0-9a-f]{64}$ ]] ||
        fail "selected Docker engine returned a malformed Python image inventory"
    [[ "$candidate_reference" == "python@$python_digest" || \
        "$candidate_reference" == "docker.io/library/python@$python_digest" ]] || continue
    [[ -z "$docker_base_id" || "$docker_base_id" == "$candidate_id" ]] ||
        fail "selected Docker engine returned conflicting IDs for the pinned Python base"
    docker_base_id="$candidate_id"
done <<< "$docker_base_listing"

docker_base_image=""
if [[ -n "$docker_base_id" ]]; then
    docker_base_digests="$(docker --host "$NMRPEAK_DOCKER_HOST" image inspect "$python_ref" \
        --format '{{range .RepoDigests}}{{println .}}{{end}}')"
    [[ "$docker_base_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "selected Docker engine returned an invalid Python base image ID"
    digest_found=0
    while IFS= read -r observed_digest; do
        if [[ "$observed_digest" == "python@$python_digest" || \
            "$observed_digest" == "docker.io/library/python@$python_digest" ]]; then
            digest_found=1
        fi
    done <<< "$docker_base_digests"
    (( digest_found == 1 )) || fail "selected Docker engine resolved the pinned Python base without its exact repository digest"
    docker_base_inspected_id="$(docker --host "$NMRPEAK_DOCKER_HOST" image inspect "$python_ref" --format '{{.Id}}')"
    [[ "$docker_base_inspected_id" == "$docker_base_id" ]] || fail "selected Docker engine changed the Python base ID during inspection"
    docker_base_image="$docker_base_inspected_id"
else
    command -v podman >/dev/null || fail "podman is required to import the pinned Python base into the selected Docker engine"
    if ! podman --remote=false image inspect "$python_ref" >/dev/null 2>&1; then
        env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u NO_PROXY -u no_proxy \
            -u ALL_PROXY -u all_proxy -u FTP_PROXY -u ftp_proxy \
            podman --remote=false pull --quiet "$python_ref" >/dev/null
    fi
    observed_digest="$(podman --remote=false image inspect "$python_ref" --format '{{.Digest}}')"
    [[ "$observed_digest" == "$python_digest" ]] ||
        fail "Podman returned the wrong Python base digest: $observed_digest"
    podman_base_id="$(podman --remote=false image inspect "$python_ref" --format '{{.Id}}')"
    podman_alias_created=1
    podman --remote=false tag "$python_ref" "$python_alias"
    podman_alias_id="$(podman --remote=false image inspect "$python_alias" --format '{{.Id}}')"
    [[ "$podman_alias_id" == "$podman_base_id" ]] || fail "Podman changed the pinned Python base identity while tagging it for transfer"
    podman --remote=false save --format docker-archive --output "$scratch/python-base.tar" "$python_alias"
    engine_active=1
    docker --host "$NMRPEAK_DOCKER_HOST" load --input "$scratch/python-base.tar" >/dev/null
    docker_base_id="$(docker --host "$NMRPEAK_DOCKER_HOST" image inspect "$python_alias" --format '{{.Id}}')"
    [[ "$docker_base_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "selected Docker engine returned an invalid imported Python base image ID"
    docker_base_image="$docker_base_id"
    docker_base_imported=1
    engine_active=0
fi

mkdir -p "$scratch/context/containers/test" "$scratch/context/dependencies"
cp "$repo_root/requirements.lock" "$scratch/context/requirements.lock"
cp "$repo_root/dependencies/provider-core-api.lock" "$scratch/context/dependencies/provider-core-api.lock"
cp "$repo_root/dependencies/provider-core-source.txt" "$scratch/context/dependencies/provider-core-source.txt"
cp "$repo_root/containers/test/os-packages.lock" "$scratch/context/containers/test/os-packages.lock"

engine_active=1
docker --host "$NMRPEAK_DOCKER_HOST" build --pull=false --network "$build_network" \
    "${proxy_build_arguments[@]}" \
    --build-arg "PYTHON_BASE_IMAGE=$docker_base_image" \
    --build-arg "TEST_BASE_IDENTITY=$base_key" \
    --tag "$test_image" \
    --file "$repo_root/containers/test/Dockerfile.base" "$scratch/context"
engine_active=0

observed_key="$(docker --host "$NMRPEAK_DOCKER_HOST" image inspect "$test_image" \
    --format '{{index .Config.Labels "io.numpde.nmrpeak.test-base-key"}}')"
[[ "$observed_key" == "$base_key" ]] || fail "built test image has the wrong dependency key"
printf '%s\n' "$test_image"
