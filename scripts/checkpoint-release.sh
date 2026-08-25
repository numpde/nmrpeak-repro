#!/usr/bin/env bash
set -euo pipefail

readonly operation=${1:-}
readonly runner=${2:-}
readonly release=${3:-}
readonly archive=${4:-}
readonly declaration=${5:-}

fail() {
    printf '%s\n' "$*" >&2
    exit 2
}

case "$runner" in
    nmrpeak_chf_v1) readonly module=repository_checks.chf_release ;;
    nmrpeak_hf_v1) readonly module=repository_checks.hf_release ;;
    *) fail "checkpoint release runner is not supported: ${runner:-<unset>}" ;;
esac

readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
declare -a arguments=(
    "$operation"
    --runner "$runner"
    --release "$release"
    --archive "$archive"
)
if [[ "$operation" == check || "$operation" == install ]]; then
    [[ -n "$declaration" ]] || fail "checkpoint release $operation requires a declaration"
    arguments+=(--declaration "$declaration")
elif [[ "$operation" != write || -n "$declaration" ]]; then
    fail "checkpoint release operation must be write, check, or install"
fi

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" \
    "${PYTHON:-python3}" -m "$module" "${arguments[@]}"
