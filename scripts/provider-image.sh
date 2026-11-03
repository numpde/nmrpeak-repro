#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf '%s\n' "$*" >&2
    exit 2
}

readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
source "$repo_root/scripts/image-operation-lock.sh"
hold_image_operation_lock "$repo_root"
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
tool_metadata="$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" "$python" -c 'from repository_checks.wheelhouse_tool_asset import TOOL_NAME, TOOL_SHA256; print(TOOL_NAME, TOOL_SHA256)')"
read -r tool_name tool_sha256 <<<"$tool_metadata"
readonly tool="$wheelhouse/$tool_name"

scratch="$(image_capture_directory "$repo_root")"
readonly report_container="nmrpeak-wheel-report-${scratch##*.}"
engine_active=0
build_inputs_streamed=0
worker_cid=''
worker_label=''
cleanup() {
    local status=$?
    trap - EXIT
    if (( engine_active )) && [[ -n "$worker_cid" ]] && retire_image_worker "$worker_cid" "$worker_label"; then
        engine_active=0
    fi
    if (( engine_active )); then
        if (( build_inputs_streamed )) && ! trim_streamed_image_capture "$scratch"; then
            printf 'Cannot confirm removal of streamed image build inputs at %s.\n' "$scratch" >&2
        fi
        printf 'Build or report retirement is unconfirmed; preserve the capture at %s and inspect Docker (report container %s) before cleanup or retrying.\n' "$scratch" "$report_container" >&2
    else
        if ! rm -rf -- "$scratch"; then
            printf 'Cannot confirm removal of image build inputs at %s.\n' "$scratch" >&2
            (( status != 0 )) || status=1
        fi
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
base_id="$(docker image inspect "$base" --format '{{.Id}}')" ||
    fail 'the pinned Python base image must already exist on the selected Docker engine'
[[ "$base_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'the pinned Python base image has an invalid Docker identity'
printf '%s\n' "$base_id" >"$scratch/base.id"
operator_uid="$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" "$python" -m deployment.operator_container_uid "$base_id")"
readonly operator_uid
lock_arguments=()
for path in requirements.lock dependencies/provider-core-api.lock dependencies/provider-core-source.txt; do
    read -r digest _ < <(sha256sum -- "$context/$path")
    lock_arguments+=(--lock-sha256 "$digest")
    install -m 0644 -- "$context/$path" "$report_input/$path"
done
engine_active=1
worker_cid="$scratch/report.cid"
worker_label="${scratch##*.}"
set +e
docker run --name "$report_container" --cidfile "$scratch/report.cid" \
    --label "io.numpde.nmrpeak.wheel-report=${scratch##*.}" \
    --platform linux/amd64 --pull never --network none --read-only --user "$operator_uid:65532" --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 64 --memory 512m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
    --mount "type=bind,src=$report_input,dst=/input,readonly" \
    --mount "type=bind,src=$wheelhouse,dst=/wheelhouse,readonly" \
    --entrypoint python "$base" -I -c "$(<"$scratch/report.py")" \
    --source /input --pool /wheelhouse "${lock_arguments[@]}" >"$scratch/report.json"
report_status=$?
set -e
retire_image_worker "$worker_cid" "$worker_label" || fail 'The wheel report worker could not be confirmed retired.'
engine_active=0
worker_cid=''
[[ "$report_status" == 0 && "$image_worker_exit_code" == 0 ]] || fail 'The wheel report worker failed.'
"$python" -I -S "$tool" report --report "$scratch/report.json" --pool /wheelhouse --directory wheels >"$scratch/selection.json"
readonly selection_id="$("$python" -I -S "$tool" identity --manifest "$scratch/selection.json")"
[[ "$selection_id" =~ ^[0-9a-f]{64}$ ]] || fail 'shared wheelhouse tool returned an invalid selection identity'
read -r build_identity _ < <(printf 'nmrpeak-provider-build-v1\n%s\n%s\n%s\n%s\n%s\n' \
    "$revision" "$image_input_id" "$selection_id" "$report_helper_hash" "$tool_sha256" | sha256sum)
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
build_inputs_streamed=1
[[ "${pipeline_status[1]}" != 0 ]] || engine_active=0
[[ "${pipeline_status[0]}" == 0 && "${pipeline_status[1]}" == 0 ]] ||
    fail "provider input stream or build failed (stream ${pipeline_status[0]}, build ${pipeline_status[1]})"
engine_active=0
build_inputs_streamed=0

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

engine_active=1
worker_cid="$scratch/smoke.cid"
worker_label="${scratch##*.}-smoke"
set +e
docker run --name "nmrpeak-image-smoke-${scratch##*.}" --cidfile "$worker_cid" \
    --label "io.numpde.nmrpeak.wheel-report=$worker_label" --network none --entrypoint python "$image_id" \
    -P -B -c 'import cryptography; import provider_core.controller; import nmrpeak_provider.provider_main'
smoke_status=$?
set -e
retire_image_worker "$worker_cid" "$worker_label" || fail 'The image import worker could not be confirmed retired.'
engine_active=0
worker_cid=''
[[ "$smoke_status" == 0 && "$image_worker_exit_code" == 0 ]] || fail 'The image import worker failed.'
docker image tag "$image_id" "$selected_image"
docker image tag "$image_id" "$image"
printf 'Built %s from %s as %s\n' "$image" "$image_input_id" "$image_id"
