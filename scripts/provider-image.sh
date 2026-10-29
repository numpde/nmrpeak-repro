#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf '%s\n' "$*" >&2
    exit 2
}

readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
readonly python="${PYTHON:-python3}"
readonly revision="$(git -C "$repo_root" rev-parse --verify HEAD)"
source "$repo_root/scripts/network-interface.sh"
select_network_interface
readonly network_interface="$NETWORK_INTERFACE_SELECTED"
require_local_docker
docker() { command docker --context default "$@"; }
[[ -z "$(git -C "$repo_root" status --short)" ]] ||
    fail "cannot build the provider from a checkout with uncommitted changes"

scratch="$(mktemp -d)"
proxy_pid=''
engine_active=0
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$proxy_pid" ]] && (( ! engine_active )); then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    if (( engine_active )); then
        printf 'Build retirement is unconfirmed; preserve inputs at %s and proxy PID %s; the proxy may remain accessible to local processes. Inspect Docker and the process before cleanup or retrying.\n' "$scratch" "${proxy_pid:-none}" >&2
    else
        rm -rf -- "$scratch"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

readonly context="$scratch/context"
install -d -m 0700 -- "$context"
readonly image_input_id="$({
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" \
        "$python" -m repository_checks.nmrpeak_image_inputs \
        provider "$repo_root" "$revision" "$context"
})"

build_network=default
proxy_build_arguments=(--build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= --build-arg http_proxy= --build-arg https_proxy= --build-arg NO_PROXY= --build-arg no_proxy= --build-arg ALL_PROXY= --build-arg all_proxy= --build-arg FTP_PROXY= --build-arg ftp_proxy=)
if [[ "$network_interface" != system ]]; then
    build_network=host
    readonly ready_file="$scratch/proxy.port"
    setsid "$python" "$repo_root/docker/bound_http_proxy.py" \
        --interface="$network_interface" --ready-file "$ready_file" </dev/null >/dev/null 2>&1 &
    proxy_pid=$!
    for _ in {1..100}; do
        [[ -s "$ready_file" ]] && break
        kill -0 "$proxy_pid" 2>/dev/null || fail "Network proxy stopped before the provider build."
        sleep 0.05
    done
    [[ -s "$ready_file" ]] || fail "Network proxy did not become ready before the provider build."
    readonly proxy_url="http://127.0.0.1:$(<"$ready_file")"
    proxy_build_arguments=(
        --build-arg "HTTP_PROXY=$proxy_url"
        --build-arg "HTTPS_PROXY=$proxy_url"
        --build-arg "http_proxy=$proxy_url"
        --build-arg "https_proxy=$proxy_url"
        --build-arg "NO_PROXY="
        --build-arg "no_proxy="
        --build-arg "ALL_PROXY="
        --build-arg "all_proxy="
        --build-arg "FTP_PROXY="
        --build-arg "ftp_proxy="
    )
fi
readonly image="numpde/nmrpeak-provider:${image_input_id#sha256:}"
readonly image_id_file="$scratch/image.id"
engine_active=1
docker build --network "$build_network" "${proxy_build_arguments[@]}" \
    --build-arg "SOURCE_REVISION=$revision" --build-arg "IMAGE_INPUT_ID=$image_input_id" \
    --iidfile "$image_id_file" --tag "$image" \
    --file "$context/containers/provider/Dockerfile" "$context"
engine_active=0

readonly image_id="$(<"$image_id_file")"
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    fail "Docker returned a malformed provider image ID: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "$revision" ]] ||
    fail "built provider image does not record its committed source revision: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.image.input-id"}}')" == "$image_input_id" ]] ||
    fail "built provider image does not record its exact input identity: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{.Config.User}}')" == 65532:65532 ]] ||
    fail "built provider image does not use the fixed non-root identity: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.provider.contract-id"}}')" == nmr.provider.http.v1 ]] ||
    fail "built provider image does not record its provider HTTP contract: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{json .Config.Entrypoint}}')" == '["python","-m","nmrpeak_provider.provider_main"]' ]] ||
    fail "built provider image does not use its fixed entrypoint: $image_id"

docker run --rm --network none --entrypoint python "$image_id" \
    -P -c 'import cryptography; import provider_core.controller; import nmrpeak_provider.provider_main'
printf 'Built %s from %s as %s\n' "$image" "$image_input_id" "$image_id"
