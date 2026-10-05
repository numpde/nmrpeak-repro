#!/usr/bin/env bash
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 2; }
[[ $# -ge 9 ]] || fail "usage: container-live-test.sh <root> <image> <project> <source-revision> <credential> <state> <ca-or-empty> <module> -- <arguments...>"
readonly repo_root="$1" image="$2" project="$3" source_revision="$4" credential="$5" state="$6" ca="$7" module="$8"
shift 8
[[ "$1" == -- ]] || fail "live test arguments must follow --"
shift
readonly test_uid="$(id -u)" test_gid="$(id -g)"
[[ "$test_uid" != 0 && "$test_gid" != 0 && "$test_uid" != 65532 ]] ||
    fail "containerized tests require a non-root user distinct from provider UID 65532"
endpoint="$(docker --context default context inspect default --format '{{.Endpoints.docker.Host}}')"
[[ "$endpoint" == unix:///* ]] || fail "Docker context default is not local: $endpoint"
[[ "$source_revision" =~ ^[0-9a-f]{40}$ ]] ||
    fail "LIVE_SOURCE_REVISION must be a lowercase 40-character Git revision"
observed_revision="$(git -C "$repo_root" rev-parse --verify HEAD)"
[[ "$observed_revision" == "$source_revision" ]] ||
    fail "LIVE_SOURCE_REVISION does not equal this checkout's HEAD: $observed_revision"
source_status="$(git -C "$repo_root" status --porcelain=v1 --untracked-files=all)"
[[ -z "$source_status" ]] || {
    printf '%s\n' 'live tests require a clean reviewed checkout; current changes:' >&2
    printf '%s\n' "$source_status" >&2
    exit 2
}

[[ "$credential" == /* && -f "$credential" && ! -L "$credential" ]] ||
    fail "LIVE_USER_CREDENTIAL must be an absolute regular non-symlink file"
[[ "$(realpath -e -- "$credential")" == "$credential" ]] ||
    fail "LIVE_USER_CREDENTIAL path must not traverse symlinks or aliases"
[[ "$(stat -c '%u' "$credential")" == "$test_uid" && "$(stat -c '%a' "$credential")" =~ ^(400|600)$ ]] ||
    fail "LIVE_USER_CREDENTIAL must be owned by the invoking user and owner-only"
[[ "$state" == /* && "$(basename -- "$state")" != . && "$(basename -- "$state")" != .. ]] ||
    fail "LIVE_STATE must be an absolute file path"
readonly state_directory="$(dirname -- "$state")"
[[ -d "$state_directory" && ! -L "$state_directory" ]] ||
    fail "LIVE_STATE parent must be a real directory"
[[ "$(realpath -e -- "$state_directory")" == "$state_directory" ]] ||
    fail "LIVE_STATE parent path must not traverse symlinks or aliases"
[[ "$(stat -c '%u' "$state_directory")" == "$test_uid" && "$(stat -c '%a' "$state_directory")" =~ ^700$ ]] ||
    fail "LIVE_STATE parent must be owned by the invoking user with mode 0700"
if [[ -e "$state" || -L "$state" ]]; then
    [[ -f "$state" && ! -L "$state" && "$(stat -c '%u:%a' "$state")" == "$test_uid:600" ]] ||
        fail "existing LIVE_STATE must be an owner-only regular non-symlink file"
fi
state_name="$(basename -- "$state")"
while IFS= read -r -d '' entry; do
    entry_name="$(basename -- "$entry")"
    case "$entry_name" in
        "$state_name"|".$state_name.lock") ;;
        *) fail "LIVE_STATE parent must be dedicated to this state; unexpected entry: $entry_name" ;;
    esac
done < <(find "$state_directory" -mindepth 1 -maxdepth 1 -print0)
if [[ -n "$ca" ]]; then
    [[ "$ca" == /* && -f "$ca" && ! -L "$ca" ]] ||
        fail "LIVE_CA_CERTIFICATE must be an absolute regular non-symlink file"
    [[ "$(realpath -e -- "$ca")" == "$ca" ]] ||
        fail "LIVE_CA_CERTIFICATE path must not traverse symlinks or aliases"
fi

readonly scratch="$(mktemp -d)"
readonly live_project="$project-live-$$"
compose_started=0
cleanup() {
    status=$?
    trap - EXIT
    cleanup_failed=0
    if (( compose_started )); then
        if ! env NMRPEAK_TEST_IMAGE="$image" NMRPEAK_TEST_UID="$test_uid" NMRPEAK_TEST_GID="$test_gid" \
                NMRPEAK_TEST_CHECKOUT="$snapshot" NMRPEAK_LIVE_CREDENTIAL="$credential" \
                NMRPEAK_LIVE_STATE_DIRECTORY="$state_directory" \
                NMRPEAK_LIVE_CA_CERTIFICATE="${ca:-/dev/null}" \
                docker --context default compose --env-file /dev/null -p "$live_project" \
                    -f "$repo_root/compose/test.yml" down --remove-orphans; then
            cleanup_failed=1
            printf 'live-test Docker project cleanup failed; project=%s snapshot=%s\n' \
                "$live_project" "$snapshot" >&2
            printf 'Inspect with: docker --context default ps --all --filter label=com.docker.compose.project=%s\n' \
                "$live_project" >&2
            (( status != 0 )) || status=2
        fi
    fi
    if (( ! cleanup_failed )); then
        rm -rf -- "$scratch"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
readonly snapshot="$scratch/repository"
snapshot_digest="$(python3 -P "$repo_root/repository_checks/test_snapshot.py" "$repo_root" "$snapshot")"
printf 'Live-test source snapshot: %s\n' "$snapshot_digest"
post_revision="$(git -C "$repo_root" rev-parse --verify HEAD)"
post_status="$(git -C "$repo_root" status --porcelain=v1 --untracked-files=all)"
[[ "$post_revision" == "$source_revision" && -z "$post_status" ]] ||
    fail "checkout changed while the reviewed live-test snapshot was materialized"
snapshot_revision="$(git -C "$snapshot" rev-parse --verify HEAD)"
snapshot_status="$(git -C "$snapshot" status --porcelain=v1 --untracked-files=all)"
[[ "$snapshot_revision" == "$source_revision" && -z "$snapshot_status" ]] ||
    fail "live-test snapshot does not exactly match the reviewed clean revision"
rm -rf -- "$snapshot/.git" "$snapshot/nmrpeak-upstream/.git" "$snapshot/unicore-upstream/.git"

compose_started=1
env NMRPEAK_TEST_IMAGE="$image" NMRPEAK_TEST_UID="$test_uid" NMRPEAK_TEST_GID="$test_gid" \
    NMRPEAK_TEST_CHECKOUT="$snapshot" NMRPEAK_LIVE_CREDENTIAL="$credential" \
    NMRPEAK_LIVE_STATE_DIRECTORY="$state_directory" \
    NMRPEAK_LIVE_CA_CERTIFICATE="${ca:-/dev/null}" \
    docker --context default compose --env-file /dev/null -p "$live_project" \
        -f "$repo_root/compose/test.yml" run --pull never --rm --no-deps --no-TTY \
        live-test-runner -m "$module" "$@"
