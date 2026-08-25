#!/usr/bin/env bash
set -euo pipefail

readonly operation=${1:-}

fail() {
    printf '%s\n' "$*" >&2
    exit 2
}

case "$operation" in
    import)
        readonly runner=${2:-}
        case "$runner" in
            nmrpeak_chf_v1) readonly module=repository_checks.chf_checkpoint ;;
            nmrpeak_hf_v1) readonly module=repository_checks.hf_checkpoint ;;
            *) fail "checkpoint import runner is not supported: ${runner:-<unset>}" ;;
        esac
        arguments=(
            import
            --runner "$runner"
            --release "${3:-}"
            --archive "${4:-}"
        )
        ;;
    recover)
        readonly volume=${2:-}
        case "$volume" in
            nmrpeak-chf-checkpoint-*) readonly module=repository_checks.chf_checkpoint ;;
            nmrpeak-hf-checkpoint-*) readonly module=repository_checks.hf_checkpoint ;;
            *) fail "checkpoint recovery volume is not owned by an NMRPeak lane: ${volume:-<unset>}" ;;
        esac
        arguments=(recover --volume "$volume" --confirm "${3:-}")
        ;;
    *) fail "checkpoint volume operation must be import or recover" ;;
esac

readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" \
    "${PYTHON:-python3}" -m "$module" "${arguments[@]}"
