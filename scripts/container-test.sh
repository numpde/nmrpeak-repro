#!/usr/bin/env bash
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 2; }
[[ $# -eq 4 ]] || fail "usage: container-test.sh <repository-root> <image> <project> <lane>"
readonly repo_root="$1" image="$2" project="$3" lane="$4"
readonly test_uid="$(id -u)" test_gid="$(id -g)"
[[ "$test_uid" != 0 && "$test_gid" != 0 && "$test_uid" != 65532 ]] ||
    fail "containerized tests require a non-root user distinct from provider UID 65532"
endpoint="$(docker --context default context inspect default --format '{{.Endpoints.docker.Host}}')"
[[ "$endpoint" == unix:///* ]] || fail "Docker context default is not local: $endpoint"

readonly scratch="$(mktemp -d)"
cleanup() { status=$?; trap - EXIT; rm -rf -- "$scratch"; exit "$status"; }
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
readonly snapshot="$scratch/repository"
snapshot_digest="$(python3 -P "$repo_root/repository_checks/test_snapshot.py" "$repo_root" "$snapshot")"
printf 'Test source snapshot: %s\n' "$snapshot_digest"

compose=(docker --context default compose --env-file /dev/null
    -p "$project" -f "$repo_root/compose/test.yml")
run_test() {
    env NMRPEAK_TEST_IMAGE="$image" NMRPEAK_TEST_UID="$test_uid" \
        NMRPEAK_TEST_GID="$test_gid" NMRPEAK_TEST_CHECKOUT="$snapshot" \
        "${compose[@]}" run --pull never --rm --no-deps --no-TTY "$@"
}
run_unittest() { run_test test-runner "/workspace/tests/$1" --top-level /workspace; }
run_model() {
    run_test test-runner /workspace/tests/model_behavior --top-level /workspace \
        --pattern test_run_interpreter.py
    run_test --entrypoint python test-runner -P \
        /workspace/tests/model_behavior/run_interpreter.py --lane hf --list
    run_test --entrypoint python test-runner -P \
        /workspace/tests/model_behavior/run_interpreter.py --lane chf --list
}

case "$lane" in
    unit|contract|integration|repository) run_unittest "$lane" ;;
    model-behavior-fixtures) run_model ;;
    all)
        run_unittest unit
        run_unittest contract
        run_unittest integration
        run_model
        run_unittest repository
        ;;
    *) fail "unknown test lane: $lane" ;;
esac
