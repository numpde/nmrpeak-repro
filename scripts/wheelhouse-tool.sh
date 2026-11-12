#!/usr/bin/env bash
set -euo pipefail

readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
readonly python="${PYTHON:-python3}"
[[ "${NMRPEAK_WHEELHOUSE_INPUT_ORIGIN:-}" == 'command line' && -n "${NMRPEAK_WHEELHOUSE_INPUT:-}" ]] || {
    printf '%s\n' 'Set WHEELHOUSE to the existing shared wheelhouse directory on the command line.' >&2
    exit 2
}
[[ -d "$NMRPEAK_WHEELHOUSE_INPUT" && ! -L "$NMRPEAK_WHEELHOUSE_INPUT" ]] || {
    printf '%s\n' 'WHEELHOUSE must name an existing directory, not a symlink.' >&2
    exit 2
}
wheelhouse=''
IFS= read -r -d '' wheelhouse < <(realpath -e -z -- "$NMRPEAK_WHEELHOUSE_INPUT") || {
    printf '%s\n' 'Could not resolve WHEELHOUSE as a complete path.' >&2
    exit 2
}
[[ "$wheelhouse" != / && "$wheelhouse" != *','* && "$wheelhouse" != *$'\n'* && "$wheelhouse" != *$'\r'* ]] || {
    printf '%s\n' 'WHEELHOUSE must be a selected subdirectory path without commas or line breaks.' >&2
    exit 2
}

set +e
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" "$python" -m repository_checks.wheelhouse_tool_asset check "$wheelhouse"
status=$?
set -e
if [[ "$status" == 0 ]]; then
    printf 'Shared wheelhouse tool is already verified in %s.\n' "$wheelhouse"
    exit 0
fi
[[ "$status" == 10 ]] || exit "$status"
source "$repo_root/scripts/network-interface.sh"
select_network_interface
if [[ -z "${NMRPEAK_DOCKER_HOST:-}" ]]; then
    NMRPEAK_DOCKER_HOST="$(bash "$repo_root/scripts/docker-context.sh" \
        "${NMRPEAK_DOCKER_CONTEXT_INPUT_ORIGIN:-undefined}" "${NMRPEAK_DOCKER_CONTEXT_INPUT:-}")"
    export NMRPEAK_DOCKER_HOST
fi
require_local_docker
[[ "$repo_root" != *','* && "$repo_root" != *$'\n'* && "$repo_root" != *$'\r'* ]] || {
    printf '%s\n' 'The checkout path must not contain commas or line breaks.' >&2; exit 2;
}
readonly base='docker.io/library/python:3.12.16-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487'
base_id="$(docker --host "$NMRPEAK_DOCKER_HOST" image inspect "$base" --format '{{.Id}}')" || {
    printf '%s\n' 'The pinned Python base must already exist on the selected Docker engine.' >&2; exit 2;
}
[[ "$base_id" =~ ^sha256:[0-9a-f]{64}$ ]] || { printf '%s\n' 'The pinned Python base identity is invalid.' >&2; exit 2; }
scratch="$(mktemp -d)"
retired_receipt="$scratch/retired"
cleanup() {
    local status=$?
    trap - EXIT
    if [[ ! -f "$retired_receipt" || -L "$retired_receipt" ]] || ! cmp -s -- "$retired_receipt" <(printf 'retired\n'); then
        printf 'Acquisition retirement is unconfirmed; preserve %s and inspect the shared owner evidence before retrying.\n' "$scratch" >&2
        (( status != 0 )) || status=1
    elif ! rm -rf -- "$scratch"; then
        printf 'Cannot remove acquisition inputs at %s.\n' "$scratch" >&2
        (( status != 0 )) || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
fetch_tool() {
    acquisition_set_route "$NETWORK_INTERFACE_SELECTED" || exit "$?"
    local -a grants=(--platform linux/amd64 --pull never --read-only
        --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 512m
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m
        --mount "type=bind,src=$wheelhouse,dst=/wheelhouse"
        --mount "type=bind,src=$repo_root/repository_checks/__init__.py,dst=/work/repository_checks/__init__.py,readonly"
        --mount "type=bind,src=$repo_root/repository_checks/wheelhouse_tool_asset.py,dst=/work/repository_checks/wheelhouse_tool_asset.py,readonly"
        --mount "type=bind,src=$repo_root/repository_checks/wheelhouse_tool_fetch.py,dst=/work/repository_checks/wheelhouse_tool_fetch.py,readonly"
        --entrypoint python)
    local -a application=(-I -B -c
        'import sys; sys.path.insert(0, "/work"); from repository_checks.wheelhouse_tool_fetch import main; main()' /wheelhouse)
    local status
    acquisition_worker online status "$base" grants application || exit "$?"
    (( status == 0 )) || exit "$status"
}
"$python" -I -S -B "$repo_root/bootstrap/acquisition/acquisition_tool/source_update.py" verify --source-dir "$repo_root/bootstrap/acquisition" || {
    printf '%s\n' 'Cannot verify the shared acquisition bootstrap.' >&2; exit 2;
}
source "$repo_root/bootstrap/acquisition/acquisition_tool/owner.sh"
acquisition_scope --endpoint "$NMRPEAK_DOCKER_HOST" --bootstrap-image "$base" \
    --retired-receipt "$retired_receipt" -- fetch_tool
[[ -f "$retired_receipt" && ! -L "$retired_receipt" ]] && cmp -s -- "$retired_receipt" <(printf 'retired\n') || {
    printf '%s\n' 'Acquisition retirement is unconfirmed; no success can be reported.' >&2; exit 2;
}
printf 'Shared wheelhouse tool is verified in %s.\n' "$wheelhouse"
