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
readonly python_alias="localhost/nmrpeak-test/python-base:${python_digest#sha256:}"
readonly base_key="$("$repo_root/scripts/test-image-key.sh" "$repo_root")"
readonly test_image="nmrpeak-repro/python-test:$base_key"

endpoint="$(docker --context default context inspect default --format '{{.Endpoints.docker.Host}}')"
case "$endpoint" in
    unix:///*) ;;
    *) fail "Docker context default must use a local Unix socket" ;;
esac

existing_id="$(docker --context default image ls --quiet --no-trunc "$test_image")"
if [[ -n "$existing_id" ]]; then
    existing_key="$(docker --context default image inspect "$existing_id" \
        --format '{{index .Config.Labels "io.numpde.nmrpeak.test-base-key"}}')"
    [[ "$existing_key" == "$base_key" ]] || fail "existing test image has the wrong dependency key"
    printf '%s\n' "$test_image"
    exit 0
fi

readonly scratch="$(mktemp -d)"
proxy_pid=""
engine_active=0
cleanup() {
    status=$?
    trap - EXIT
    if [[ -n "$proxy_pid" ]]; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    if (( engine_active )); then
        printf 'Build retirement is unconfirmed; preserve inputs at %s and inspect Docker before retrying.\n' "$scratch" >&2
    else
        rm -rf -- "$scratch"
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
    "${PYTHON:-python3}" "$repo_root/docker/bound_http_proxy.py" \
        --interface="$network_interface" --ready-file "$scratch/proxy.port" &
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

command -v podman >/dev/null || fail "podman is required to authenticate the pinned Python base through selected interface"
if ! podman --remote=false image inspect "$python_ref" >/dev/null 2>&1; then
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u NO_PROXY -u no_proxy \
        -u ALL_PROXY -u all_proxy -u FTP_PROXY -u ftp_proxy \
        podman --remote=false pull --quiet "$python_ref" >/dev/null
fi
observed_digest="$(podman --remote=false image inspect "$python_ref" --format '{{.Digest}}')"
[[ "$observed_digest" == "$python_digest" ]] ||
    fail "Podman returned the wrong Python base digest: $observed_digest"
expected_base_id="sha256:$(podman --remote=false image inspect "$python_ref" --format '{{.Id}}')"
docker_base_id=""
docker_base_match="$(docker --context default image ls --quiet --no-trunc "$python_alias")"
if [[ -n "$docker_base_match" ]]; then
    docker_base_id="$(docker --context default image inspect "$docker_base_match" --format '{{.Id}}')"
fi
if [[ "$docker_base_id" != "$expected_base_id" ]]; then
    podman --remote=false tag "$python_ref" "$python_alias"
    podman --remote=false save --format docker-archive --output "$scratch/python-base.tar" "$python_alias"
    docker --context default load --input "$scratch/python-base.tar" >/dev/null
fi

mkdir -p "$scratch/context/containers/test" "$scratch/context/dependencies"
cp "$repo_root/requirements.lock" "$scratch/context/requirements.lock"
cp "$repo_root/dependencies/provider-core-api.lock" "$scratch/context/dependencies/provider-core-api.lock"
cp "$repo_root/dependencies/provider-core-source.txt" "$scratch/context/dependencies/provider-core-source.txt"
cp "$repo_root/containers/test/os-packages.lock" "$scratch/context/containers/test/os-packages.lock"

engine_active=1
docker --context default build --pull=false --network "$build_network" \
    "${proxy_build_arguments[@]}" \
    --build-arg "PYTHON_BASE_IMAGE=$python_alias" \
    --build-arg "TEST_BASE_IDENTITY=$base_key" \
    --tag "$test_image" \
    --file "$repo_root/containers/test/Dockerfile.base" "$scratch/context"
engine_active=0

observed_key="$(docker --context default image inspect "$test_image" \
    --format '{{index .Config.Labels "io.numpde.nmrpeak.test-base-key"}}')"
[[ "$observed_key" == "$base_key" ]] || fail "built test image has the wrong dependency key"
printf '%s\n' "$test_image"
