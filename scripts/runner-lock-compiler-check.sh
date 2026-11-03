#!/usr/bin/env bash
# Exercise the actual pinned lock compiler without downloading packages.
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 2; }
[[ "${1:-}" == cpu-x86_64 ]] || fail 'compiler check target must be cpu-x86_64'
readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
readonly python="${PYTHON:-python3}"
[[ "${NMRPEAK_WHEELHOUSE_INPUT_ORIGIN:-}" == 'command line' && -n "${NMRPEAK_WHEELHOUSE_INPUT:-}" ]] ||
    fail 'set WHEELHOUSE to the existing shared wheelhouse directory on the command line'
[[ -d "$NMRPEAK_WHEELHOUSE_INPUT" && ! -L "$NMRPEAK_WHEELHOUSE_INPUT" ]] || fail 'WHEELHOUSE must be an existing non-symlink directory'
wheelhouse=''
IFS= read -r -d '' wheelhouse < <(realpath -e -z -- "$NMRPEAK_WHEELHOUSE_INPUT") || fail 'could not resolve WHEELHOUSE'
[[ "$wheelhouse" != / && "$wheelhouse" != *','* && "$wheelhouse" != *$'\n'* && "$wheelhouse" != *$'\r'* ]] ||
    fail 'WHEELHOUSE must be a selected subdirectory path without commas or line breaks'
readonly wheel="$wheelhouse/certifi-2026.7.22-py3-none-any.whl"
readonly expected=62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775
[[ -f "$wheel" && ! -L "$wheel" ]] || fail 'the pinned certifi fixture wheel is missing from WHEELHOUSE'
read -r actual _ < <(sha256sum -- "$wheel")
[[ "$actual" == "$expected" ]] || fail 'the certifi fixture wheel differs from its lock hash'
source "$repo_root/scripts/network-interface.sh"
require_local_docker
docker() { command docker --host "$NMRPEAK_DOCKER_HOST" "$@"; }

scratch="$(mktemp -d)"
engine_active=0
cleanup() {
    local status=$?
    trap - EXIT
    if (( engine_active )); then
        printf 'Compiler check retirement is unconfirmed; preserve %s and inspect compiler CID and nmrpeak-compiler-check-%s before cleanup.\n' "$scratch" "${scratch##*.}" >&2
    else
        rm -rf -- "$scratch"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
chmod 0711 -- "$scratch"
install -d -m 0755 -- "$scratch/context/containers/runner-lock" "$scratch/context/families/nmrpeak/targets/cpu-x86_64"
install -d -m 0700 -- "$scratch/out"
cp -- "$repo_root/containers/runner-lock/seed-and-compile.sh" "$scratch/context/containers/runner-lock/"
cp -- "$repo_root/families/nmrpeak/targets/cpu-x86_64/Dockerfile.lock" "$scratch/context/families/nmrpeak/targets/cpu-x86_64/"
printf 'certifi @ file:///wheelhouse/certifi-2026.7.22-py3-none-any.whl\n' >"$scratch/requirements.in"
: >"$scratch/seed.lock"
chmod 0644 -- "$scratch/requirements.in" "$scratch/seed.lock"
engine_active=1
docker build --platform linux/amd64 --pull=false --network none --iidfile "$scratch/image.id" \
    --file "$scratch/context/families/nmrpeak/targets/cpu-x86_64/Dockerfile.lock" "$scratch/context"
engine_active=0
image_id="$(<"$scratch/image.id")"
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'Docker returned an invalid compiler image ID'
[[ "$(docker image inspect "$image_id" --format '{{.Os}}/{{.Architecture}}')" == linux/amd64 ]] ||
    fail 'the compiler check image is not Linux amd64'
operator_uid="$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" "$python" -m deployment.operator_container_uid "$image_id")"
engine_active=1
docker run --name "nmrpeak-compiler-check-${scratch##*.}" --cidfile "$scratch/compiler.cid" \
    --label "io.numpde.nmrpeak.compiler-check=${scratch##*.}" \
    --rm --platform linux/amd64 --pull never --network none --read-only --user "$operator_uid:65532" \
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 512m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
    --mount "type=bind,src=$scratch/requirements.in,dst=/input/requirements.in,readonly" \
    --mount "type=bind,src=$scratch/seed.lock,dst=/seed/requirements.lock,readonly" \
    --mount "type=bind,src=$scratch/out,dst=/out" \
    --mount "type=bind,src=$wheel,dst=/wheelhouse/certifi-2026.7.22-py3-none-any.whl,readonly" \
    "$image_id" /input/requirements.in --offline --no-index --output-file /out/requirements.lock
engine_active=0
grep -Fq 'certifi @ file:///wheelhouse/certifi-2026.7.22-py3-none-any.whl' "$scratch/out/requirements.lock" ||
    fail 'the compiler did not emit the selected fixture wheel'
grep -Fq -- "--hash=sha256:$expected" "$scratch/out/requirements.lock" || fail 'the compiler did not emit the fixture wheel hash'
printf '%s\n' 'Pinned runner lock compiler passed the offline wheel smoke check.'
