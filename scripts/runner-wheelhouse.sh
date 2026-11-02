#!/usr/bin/env bash
# Project the committed runner lock and acquire its target wheels into the shared pool.
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 2; }
[[ "${1:-}" == cpu-x86_64 ]] || fail 'runner wheelhouse target must be cpu-x86_64'
readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
readonly python="${PYTHON:-python3}"
readonly revision="$(git -C "$repo_root" rev-parse --verify HEAD)"
[[ -z "$(git -C "$repo_root" status --short)" ]] || fail 'commit runner inputs before acquiring their wheels'
[[ "${NMRPEAK_WHEELHOUSE_INPUT_ORIGIN:-}" == 'command line' && -n "${NMRPEAK_WHEELHOUSE_INPUT:-}" ]] ||
    fail 'set WHEELHOUSE to the existing shared wheelhouse directory on the command line'
[[ -d "$NMRPEAK_WHEELHOUSE_INPUT" && ! -L "$NMRPEAK_WHEELHOUSE_INPUT" ]] || fail 'WHEELHOUSE must be an existing non-symlink directory'
wheelhouse=''
IFS= read -r -d '' wheelhouse < <(realpath -e -z -- "$NMRPEAK_WHEELHOUSE_INPUT") || fail 'could not resolve WHEELHOUSE'
readonly wheelhouse
[[ "$wheelhouse" != / && "$wheelhouse" != *','* && "$wheelhouse" != *$'\n'* && "$wheelhouse" != *$'\r'* ]] ||
    fail 'WHEELHOUSE must be a selected subdirectory path without commas or line breaks'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" "$python" -m repository_checks.wheelhouse_tool_asset check "$wheelhouse" ||
    fail 'the pinned wheelhouse tool is missing or invalid; run make wheelhouse/tool first'
tool_name="$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" "$python" -c 'from repository_checks.wheelhouse_tool_asset import TOOL_NAME; print(TOOL_NAME)')"
readonly tool="$wheelhouse/$tool_name"
source "$repo_root/scripts/network-interface.sh"
select_network_interface
require_local_docker
docker() { command docker --host "$NMRPEAK_DOCKER_HOST" "$@"; }
readonly base='docker.io/library/python:3.12.16-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487'
base_id="$(docker image inspect "$base" --format '{{.Id}}')" || fail 'the pinned Python base image must already exist on the selected engine'
[[ "$base_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'the pinned Python base image has an invalid Docker identity'
operator_uid="$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" "$python" -m deployment.operator_container_uid "$base_id")"

scratch="$(mktemp -d)"
proxy_pid=''
engine_active=0
cleanup() {
    local status=$?
    trap - EXIT
    if (( engine_active )); then
        printf 'Wheelhouse worker retirement is unconfirmed; preserve %s, inspect its CID files and nmrpeak-{native,scientific}-{locate,acquire}-%s containers and proxy PID %s before cleanup or retrying.\n' "$scratch" "${scratch##*.}" "${proxy_pid:-none}" >&2
    else
        if [[ -n "$proxy_pid" ]]; then kill "$proxy_pid" 2>/dev/null || true; wait "$proxy_pid" 2>/dev/null || true; fi
        rm -rf -- "$scratch"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
chmod 0711 -- "$scratch"
git -C "$repo_root" show "$revision:families/nmrpeak/targets/cpu-x86_64/requirements.lock" >"$scratch/requirements.lock"
git -C "$repo_root" show "$revision:dependencies/provider-core-native.lock" >"$scratch/native.lock"
git -C "$repo_root" show "$revision:dependencies/provider-core-source.txt" >"$scratch/native-source.txt"
chmod 0644 -- "$scratch/requirements.lock" "$scratch/native.lock" "$scratch/native-source.txt"
native_source="$("$python" -c '
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
urls = re.findall(r"https://github\.com/numpde/nmr-provider-tools/releases/download/[^/\s]+/[^\s]+\.whl", text)
hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", text)
if len(urls) != 1 or len(hashes) != 1:
    raise SystemExit("The committed provider source must identify one released wheel and SHA-256.")
print(urls[0] + "#sha256=" + hashes[0])
' "$scratch/native-source.txt")"

network=bridge
proxy_arguments=(--env HTTP_PROXY= --env HTTPS_PROXY= --env http_proxy= --env https_proxy= --env NO_PROXY= --env no_proxy= --env ALL_PROXY= --env all_proxy= --env FTP_PROXY= --env ftp_proxy=)
if [[ "$NETWORK_INTERFACE_SELECTED" != system ]]; then
    network=host
    setsid "$python" "$repo_root/docker/bound_http_proxy.py" \
        --interface="$NETWORK_INTERFACE_SELECTED" --ready-file "$scratch/proxy.port" </dev/null >/dev/null 2>&1 &
    proxy_pid=$!
    for _ in {1..100}; do
        [[ -s "$scratch/proxy.port" ]] && break
        kill -0 "$proxy_pid" 2>/dev/null || fail 'Network proxy stopped before wheelhouse acquisition.'
        sleep 0.05
    done
    [[ -s "$scratch/proxy.port" ]] || fail 'Network proxy did not become ready for wheelhouse acquisition.'
    proxy_url="http://127.0.0.1:$(<"$scratch/proxy.port")"
    proxy_arguments=(--env "HTTP_PROXY=$proxy_url" --env "HTTPS_PROXY=$proxy_url" --env "http_proxy=$proxy_url" --env "https_proxy=$proxy_url" --env NO_PROXY= --env no_proxy= --env ALL_PROXY= --env all_proxy= --env FTP_PROXY= --env ftp_proxy=)
fi
worker=(--rm --platform linux/amd64 --pull never --network "$network" --read-only --user "$operator_uid:65532"
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 512m
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m "${proxy_arguments[@]}"
    --mount "type=bind,src=$tool,dst=/tool.pyz,readonly"
    --mount "type=bind,src=$wheelhouse,dst=/wheelhouse" --entrypoint python "$base" -I -S /tool.pyz)
for profile in native scientific; do
    if [[ "$profile" == native ]]; then
        lock="$scratch/native.lock"
        locate_arguments=(--index-url https://pypi.org/simple --source "$native_source")
    else
        lock="$scratch/requirements.lock"
        locate_arguments=()
    fi
    engine_active=1
    docker run --name "nmrpeak-$profile-locate-${scratch##*.}" --cidfile "$scratch/$profile-locate.cid" \
        --label "io.numpde.nmrpeak.wheelhouse=${scratch##*.}-$profile-locate" \
        --mount "type=bind,src=$lock,dst=/input/requirements.lock,readonly" \
        "${worker[@]}" locate --lock /input/requirements.lock "${locate_arguments[@]}" >"$scratch/pylock.$profile.toml"
    engine_active=0
    chmod 0644 -- "$scratch/pylock.$profile.toml"
    engine_active=1
    docker run --name "nmrpeak-$profile-acquire-${scratch##*.}" --cidfile "$scratch/$profile-acquire.cid" \
        --label "io.numpde.nmrpeak.wheelhouse=${scratch##*.}-$profile-acquire" \
        --mount "type=bind,src=$scratch/pylock.$profile.toml,dst=/input/pylock.toml,readonly" \
        --mount "type=bind,src=$lock,dst=/input/source.lock,readonly" \
        "${worker[@]}" acquire --lock /input/pylock.toml --source-lock /input/source.lock --pool /wheelhouse
    engine_active=0
done
printf 'Acquired the locked NMRPeak CPU wheel selection in %s.\n' "$wheelhouse"
