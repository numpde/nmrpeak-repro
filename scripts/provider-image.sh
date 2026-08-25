#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf '%s\n' "$*" >&2
    exit 2
}

readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
readonly python="${PYTHON:-python3}"
readonly revision="$(git -C "$repo_root" rev-parse --verify HEAD)"
readonly wifi_interface="${NMRPEAK_WIFI_INTERFACE:-}"
if [[ -n "$wifi_interface" && ! -d "/sys/class/net/$wifi_interface" ]]; then
    fail "cannot build the provider because the selected Wi-Fi interface does not exist: $wifi_interface"
fi
[[ -z "$(git -C "$repo_root" status --short)" ]] ||
    fail "cannot build the provider from a checkout with uncommitted changes"

scratch="$(mktemp -d)"
proxy_pid=''
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$proxy_pid" ]]; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    rm -rf -- "$scratch"
    exit "$status"
}
trap cleanup EXIT

readonly context="$scratch/context"
install -d -m 0700 -- "$context"
readonly image_input_id="$({
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" \
        "$python" -m repository_checks.nmrpeak_image_inputs \
        provider "$repo_root" "$revision" "$context"
})"

build_network=default
proxy_build_arguments=()
if [[ -n "$wifi_interface" ]]; then
    readonly ready_file="$scratch/proxy.port"
    "$python" "$repo_root/docker/bound_http_proxy.py" \
        --interface "$wifi_interface" --ready-file "$ready_file" &
    proxy_pid=$!
    for _ in {1..100}; do
        [[ -s "$ready_file" ]] && break
        kill -0 "$proxy_pid" 2>/dev/null || fail "Wi-Fi proxy stopped before the provider build."
        sleep 0.05
    done
    [[ -s "$ready_file" ]] || fail "Wi-Fi proxy did not become ready before the provider build."
    readonly proxy_url="http://127.0.0.1:$(<"$ready_file")"
    build_network=host
    proxy_build_arguments=(
        --build-arg "HTTP_PROXY=$proxy_url"
        --build-arg "HTTPS_PROXY=$proxy_url"
    )
fi
readonly image="numpde/nmrpeak-provider:${image_input_id#sha256:}"
readonly image_id_file="$scratch/image.id"
docker build --network "$build_network" "${proxy_build_arguments[@]}" \
    --build-arg "SOURCE_REVISION=$revision" --build-arg "IMAGE_INPUT_ID=$image_input_id" \
    --iidfile "$image_id_file" --tag "$image" \
    --file "$context/containers/provider/Dockerfile" "$context"

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
    -P -c 'import cryptography; import nmrpeak_provider.provider_main'
printf 'Built %s from %s as %s\n' "$image" "$image_input_id" "$image_id"
