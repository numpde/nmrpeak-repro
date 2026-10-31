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

scratch="$(mktemp -d)"
engine_active=0
cleanup() {
    local status=$?
    trap - EXIT
    if (( engine_active )); then
        printf 'Build retirement is unconfirmed; preserve inputs at %s and inspect Docker before cleanup or retrying.\n' "$scratch" >&2
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

readonly image="numpde/nmrpeak-provider:${image_input_id#sha256:}"
readonly image_id_file="$scratch/image.id"
docker image inspect 'docker.io/library/python:3.12.16-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487' >/dev/null 2>&1 ||
    fail 'the pinned Python base image must already exist on the selected Docker engine'
engine_active=1
docker build --pull=false --network none --build-context "wheelhouse=$wheelhouse" \
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
