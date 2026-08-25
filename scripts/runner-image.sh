#!/usr/bin/env bash
set -euo pipefail

readonly runner="${1:-}"
readonly target="${2:-}"

fail() {
    printf '%s\n' "$*" >&2
    exit 2
}

[[ "$target" == cpu-x86_64 ]] || fail "runner image target must be cpu-x86_64: ${target:-<unset>}"

case "$runner" in
    nmrpeak_chf_v1)
        readonly image_repository="numpde/nmrpeak-chf-runner"
        readonly contract_id="nmrpeak.runner_session.chf.v1"
        readonly entrypoint='["python","-m","models.nmrpeak_chf_v1.runner.owner_session_supervisor","5","5","--","python","-m","models.nmrpeak_chf_v1.runner.worker"]'
        ;;
    nmrpeak_hf_v1)
        readonly image_repository="numpde/nmrpeak-hf-runner"
        readonly contract_id="nmrpeak.runner_session.hf.v1"
        readonly entrypoint='["python","-m","models.nmrpeak_hf_v1.runner.owner_session_supervisor","5","5","--","python","-m","models.nmrpeak_hf_v1.runner.worker"]'
        ;;
    *)
        fail "runner image must select nmrpeak_chf_v1 or nmrpeak_hf_v1: ${runner:-<unset>}"
        ;;
esac

readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
readonly python="${PYTHON:-python3}"
readonly revision="$(git -C "$repo_root" rev-parse --verify HEAD)"
readonly wifi_interface=wlp1s0
[[ -d "/sys/class/net/$wifi_interface" ]] ||
    fail "cannot build $runner because the Wi-Fi interface does not exist: $wifi_interface"

tmp="$(mktemp -d)"
proxy_pid=''
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$proxy_pid" ]]; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    rm -rf -- "$tmp"
    exit "$status"
}
trap cleanup EXIT

readonly context="$tmp/context"
install -d -m 0700 -- "$context"
readonly image_input_id="$({
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" \
        "$python" -m repository_checks.nmrpeak_image_inputs \
        "$runner" "$repo_root" "$revision" "$context"
})"
readonly ready_file="$tmp/proxy.port"
"$python" "$repo_root/docker/bound_http_proxy.py" \
    --interface "$wifi_interface" --ready-file "$ready_file" &
proxy_pid=$!
for _ in {1..100}; do
    [[ -s "$ready_file" ]] && break
    kill -0 "$proxy_pid" 2>/dev/null || fail "Wi-Fi proxy stopped before the $runner build."
    sleep 0.05
done
[[ -s "$ready_file" ]] || fail "Wi-Fi proxy did not become ready before the $runner build."
readonly proxy_url="http://127.0.0.1:$(<"$ready_file")"
readonly image="$image_repository:${image_input_id#sha256:}"
readonly image_id_file="$tmp/image.id"

docker build --network host \
    --build-arg "HTTP_PROXY=$proxy_url" --build-arg "HTTPS_PROXY=$proxy_url" \
    --build-arg "SOURCE_REVISION=$revision" --build-arg "IMAGE_INPUT_ID=$image_input_id" \
    --iidfile "$image_id_file" --tag "$image" \
    --file "$context/models/$runner/runner/Dockerfile.runner" "$context"

readonly image_id="$(<"$image_id_file")"
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "Docker returned a malformed $runner image ID: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "$revision" ]] ||
    fail "built $runner image does not record its committed source revision: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.image.input-id"}}')" == "$image_input_id" ]] ||
    fail "built $runner image does not record its exact input identity: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{.Config.User}}')" == 65532:65532 ]] ||
    fail "built $runner image does not use the fixed non-root identity: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.runner.ref"}}')" == "$runner" ]] ||
    fail "built $runner image does not record its runner reference: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.runner.contract-id"}}')" == "$contract_id" ]] ||
    fail "built $runner image does not record its runner contract: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.runner.target"}}')" == "$target" ]] ||
    fail "built $runner image does not record its target: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{json .Config.Entrypoint}}')" == "$entrypoint" ]] ||
    fail "built $runner image does not use its fixed entrypoint: $image_id"
printf 'Built %s from %s as %s\n' "$image" "$image_input_id" "$image_id"
