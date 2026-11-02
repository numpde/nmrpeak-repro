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
require_local_docker
docker() { command docker --host "$NMRPEAK_DOCKER_HOST" "$@"; }
[[ -z "$(git -C "$repo_root" status --short)" ]] ||
    fail "cannot build the provider from a checkout with uncommitted changes"
[[ "${NMRPEAK_WHEELHOUSE_INPUT_ORIGIN:-}" == 'command line' && -n "${NMRPEAK_WHEELHOUSE_INPUT:-}" ]] ||
    fail 'set WHEELHOUSE to the existing shared wheelhouse directory on the command line'
[[ -d "$NMRPEAK_WHEELHOUSE_INPUT" && ! -L "$NMRPEAK_WHEELHOUSE_INPUT" ]] ||
    fail 'WHEELHOUSE must name an existing directory, not a symlink'
wheelhouse=''
IFS= read -r -d '' wheelhouse < <(realpath -e -z -- "$NMRPEAK_WHEELHOUSE_INPUT") ||
    fail 'could not resolve WHEELHOUSE as a complete path'
readonly wheelhouse
[[ "$wheelhouse" != / && "$wheelhouse" != *','* && "$wheelhouse" != *$'\n'* && "$wheelhouse" != *$'\r'* ]] ||
    fail 'WHEELHOUSE must be a selected subdirectory path without commas or line breaks'
[[ -r "$wheelhouse" && -x "$wheelhouse" ]] || fail 'WHEELHOUSE must be readable and traversable'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" "$python" -m repository_checks.wheelhouse_tool_asset check "$wheelhouse" ||
    fail 'the pinned wheelhouse tool is missing or invalid; run make wheelhouse/tool first'
readonly tool="$wheelhouse/wheelhouse-tool-0.5.7.pyz"

scratch="$(mktemp -d)"
readonly report_container="nmrpeak-wheel-report-${scratch##*.}"
engine_active=0
cleanup() {
    local status=$?
    trap - EXIT
    if (( engine_active )); then
        printf 'Build or report retirement is unconfirmed; preserve inputs at %s and inspect Docker (report container %s) before cleanup or retrying.\n' "$scratch" "$report_container" >&2
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
        provider "$repo_root" "$revision" "$context" \
        --source-allowlist "$scratch/source.json" --source-prefix "$context"
})"

readonly image="numpde/nmrpeak-provider:${image_input_id#sha256:}"
readonly image_id_file="$scratch/image.id"
git -C "$repo_root" show "$revision:repository_checks/image_wheel_report.py" >"$scratch/report.py"
read -r report_helper_hash _ < <(sha256sum -- "$scratch/report.py")
readonly report_input="$scratch/report-input"
install -d -m 0755 -- "$report_input" "$report_input/dependencies"
chmod 0711 -- "$scratch"
readonly base='docker.io/library/python:3.12.16-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487'
docker image inspect "$base" >/dev/null 2>&1 ||
    fail 'the pinned Python base image must already exist on the selected Docker engine'
lock_arguments=()
for path in requirements.lock dependencies/provider-core-api.lock dependencies/provider-core-source.txt; do
    read -r digest _ < <(sha256sum -- "$context/$path")
    lock_arguments+=(--lock-sha256 "$digest")
    install -m 0644 -- "$context/$path" "$report_input/$path"
done
engine_active=1
docker run --name "$report_container" --cidfile "$scratch/report.cid" \
    --label "io.numpde.nmrpeak.wheel-report=${scratch##*.}" \
    --rm --platform linux/amd64 --pull never --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 64 --memory 512m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
    --mount "type=bind,src=$report_input,dst=/input,readonly" \
    --mount "type=bind,src=$wheelhouse,dst=/wheelhouse,readonly" \
    --entrypoint python "$base" -I -c "$(<"$scratch/report.py")" \
    --source /input --pool /wheelhouse "${lock_arguments[@]}" >"$scratch/report.json"
engine_active=0
"$python" -I -S "$tool" report --report "$scratch/report.json" --pool /wheelhouse --directory wheels >"$scratch/selection.json"
readonly selection_id="$("$python" -I -S "$tool" identity --manifest "$scratch/selection.json")"
[[ "$selection_id" =~ ^[0-9a-f]{64}$ ]] || fail 'shared wheelhouse tool returned an invalid selection identity'
read -r build_identity _ < <(printf 'nmrpeak-provider-build-v1\n%s\n%s\n%s\n%s\n%s\n' \
    "$revision" "$image_input_id" "$selection_id" "$report_helper_hash" \
    '6d178096f9196e7f1724503b8fb509658061d5ac203de69719227f2fbd508a96' | sha256sum)
readonly selected_image="numpde/nmrpeak-provider-build:$build_identity"
engine_active=1
set +e
"$python" -I -S "$tool" stream --pool "$wheelhouse" --source-allowlist "$scratch/source.json" --manifest "$scratch/selection.json" |
docker build --platform linux/amd64 --pull=false --network none \
    --build-arg "SOURCE_REVISION=$revision" --build-arg "IMAGE_INPUT_ID=$image_input_id" \
    --label "io.numpde.nmrpeak.wheelhouse.selection=sha256:$selection_id" \
    --label "io.numpde.nmrpeak.build.input-id=sha256:$build_identity" \
    --iidfile "$image_id_file" --file containers/provider/Dockerfile -
pipeline_status=("${PIPESTATUS[@]}")
set -e
[[ "${pipeline_status[0]}" == 0 && "${pipeline_status[1]}" == 0 ]] ||
    fail "provider input stream or build failed (stream ${pipeline_status[0]}, build ${pipeline_status[1]})"
engine_active=0

readonly image_id="$(<"$image_id_file")"
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    fail "Docker returned a malformed provider image ID: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "$revision" ]] ||
    fail "built provider image does not record its committed source revision: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.image.input-id"}}')" == "$image_input_id" ]] ||
    fail "built provider image does not record its exact input identity: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.wheelhouse.selection"}}')" == "sha256:$selection_id" ]] ||
    fail "built provider image does not record its wheel selection: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.build.input-id"}}')" == "sha256:$build_identity" ]] ||
    fail "built provider image does not record its complete build identity: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{.Os}}/{{.Architecture}}')" == linux/amd64 ]] ||
    fail "built provider image is not Linux amd64: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{.Config.User}}')" == 65532:65532 ]] ||
    fail "built provider image does not use the fixed non-root identity: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.provider.contract-id"}}')" == nmr.provider.http.v1 ]] ||
    fail "built provider image does not record its provider HTTP contract: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{json .Config.Entrypoint}}')" == '["python","-m","nmrpeak_provider.provider_main"]' ]] ||
    fail "built provider image does not use its fixed entrypoint: $image_id"

docker run --rm --network none --entrypoint python "$image_id" \
    -P -c 'import cryptography; import provider_core.controller; import nmrpeak_provider.provider_main'
docker image tag "$image_id" "$selected_image"
docker image tag "$image_id" "$image"
printf 'Built %s from %s as %s\n' "$image" "$image_input_id" "$image_id"
