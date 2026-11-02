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
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" "$python" -m repository_checks.wheelhouse_tool_asset \
    acquire "$wheelhouse" --interface "$NETWORK_INTERFACE_SELECTED"
printf 'Shared wheelhouse tool is verified in %s.\n' "$wheelhouse"
