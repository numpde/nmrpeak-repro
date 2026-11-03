#!/usr/bin/env bash
# Project committed locks and acquire their target wheels into the shared pool.
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 2; }
case "${1:-}" in cpu-x86_64|benchmark-cu128|provider) ;; *) fail 'wheelhouse target must be cpu-x86_64, benchmark-cu128 or provider' ;; esac
readonly target="$1"
readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
readonly python="${PYTHON:-python3}"
readonly revision="$(git -C "$repo_root" rev-parse --verify HEAD)"
[[ -z "$(git -C "$repo_root" status --short)" ]] || fail 'commit acquisition inputs before acquiring their wheels'
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
        printf 'Wheelhouse worker retirement is unconfirmed; preserve %s, inspect its CID files, proxy.log and nmrpeak wheelhouse containers ending in %s and proxy PID %s before cleanup or retrying.\n' "$scratch" "${scratch##*.}" "${proxy_pid:-none}" >&2
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
if [[ "$target" == provider ]]; then
    git -C "$repo_root" show "$revision:requirements.lock" >"$scratch/composed.lock"
    git -C "$repo_root" show "$revision:dependencies/provider-core-api.lock" >"$scratch/api.lock"
    "$python" - "$scratch/composed.lock" "$scratch/consumer.lock" <<'PY'
import pathlib, sys
lines = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines(keepends=True)
directives = [line.strip() for line in lines if line.strip().startswith(("-c ", "-r "))]
if directives != ["-c dependencies/provider-core-source.txt", "-r dependencies/provider-core-api.lock"]:
    raise SystemExit("Provider requirements must reference the expected source and API locks exactly once.")
pathlib.Path(sys.argv[2]).write_text("".join(line for line in lines if line.strip() not in directives), encoding="utf-8")
PY
    profiles=(api consumer)
elif [[ "$target" == benchmark-cu128 ]]; then
    git -C "$repo_root" show "$revision:containers/benchmark/requirements.lock" >"$scratch/benchmark.lock"
    profiles=(benchmark)
else
    git -C "$repo_root" show "$revision:families/nmrpeak/targets/cpu-x86_64/requirements.lock" >"$scratch/requirements.lock"
    git -C "$repo_root" show "$revision:dependencies/provider-core-native.lock" >"$scratch/native.lock"
    profiles=(native scientific)
fi
git -C "$repo_root" show "$revision:dependencies/provider-core-source.txt" >"$scratch/native-source.txt"
chmod 0644 -- "$scratch"/*.lock "$scratch/native-source.txt"
native_source="$("$python" -c '
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
urls = re.findall(r"https://github\.com/numpde/nmr-provider-tools/releases/download/[^/\s]+/[^\s]+\.whl", text)
hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", text)
if len(urls) != 1 or len(hashes) != 1:
    raise SystemExit("The committed provider source must identify one released wheel and SHA-256.")
print(urls[0] + "#sha256=" + hashes[0])
' "$scratch/native-source.txt")"

# A successful target-interpreter report proves that the complete selection is
# already available without contacting an index. Preserve the normal acquisition
# path on a report failure; its diagnostics do not classify every miss or error.
report_input="$scratch/report-input"
mkdir -p -- "$report_input/dependencies" "$report_input/families/nmrpeak/targets/cpu-x86_64" "$report_input/containers/benchmark"
case "$target" in
    provider)
        report_profiles=(provider)
        report_paths=(requirements.lock dependencies/provider-core-api.lock dependencies/provider-core-source.txt) ;;
    cpu-x86_64)
        report_profiles=(native scientific)
        report_paths=(requirements-native.lock dependencies/provider-core-native.lock dependencies/provider-core-source.txt families/nmrpeak/targets/cpu-x86_64/requirements.lock) ;;
    benchmark-cu128)
        report_profiles=(benchmark)
        report_paths=(containers/benchmark/requirements.lock) ;;
esac
for path in "${report_paths[@]}"; do
    git -C "$repo_root" show "$revision:$path" >"$report_input/$path"
    chmod 0644 -- "$report_input/$path"
done
git -C "$repo_root" show "$revision:repository_checks/image_wheel_report.py" >"$scratch/report.py"
chmod 0755 -- "$report_input" "$report_input/dependencies" "$report_input/families" "$report_input/families/nmrpeak" \
    "$report_input/families/nmrpeak/targets" "$report_input/families/nmrpeak/targets/cpu-x86_64" \
    "$report_input/containers" "$report_input/containers/benchmark"
check_offline_selection() {
    local phase="$1" profile path digest name report_status report_cid
    local -a paths lock_arguments
    offline_complete=1
    for profile in "${report_profiles[@]}"; do
        case "$profile" in
            provider) paths=(requirements.lock dependencies/provider-core-api.lock dependencies/provider-core-source.txt) ;;
            native) paths=(requirements-native.lock dependencies/provider-core-native.lock dependencies/provider-core-source.txt) ;;
            scientific) paths=(families/nmrpeak/targets/cpu-x86_64/requirements.lock) ;;
            benchmark) paths=(containers/benchmark/requirements.lock) ;;
        esac
        lock_arguments=()
        for path in "${paths[@]}"; do
            read -r digest _ < <(sha256sum -- "$report_input/$path")
            lock_arguments+=(--lock-sha256 "$digest")
        done
        name="nmrpeak-$profile-$phase-${scratch##*.}"
        engine_active=1
        set +e
        docker run --name "$name" --cidfile "$scratch/$profile-$phase.cid" \
            --label "io.numpde.nmrpeak.wheelhouse=${scratch##*.}-$profile-$phase" \
            --platform linux/amd64 --pull never --network none --read-only --user "$operator_uid:65532" \
            --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 512m \
            --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
            --mount "type=bind,src=$report_input,dst=/input,readonly" \
            --mount "type=bind,src=$wheelhouse,dst=/wheelhouse,readonly" \
            --entrypoint python "$base" -I -c "$(<"$scratch/report.py")" \
            --source /input --pool /wheelhouse --profile "$profile" "${lock_arguments[@]}" \
            >"$scratch/$profile-$phase.json" 2>"$scratch/$profile-$phase.log"
        report_status=$?
        set -e
        [[ "$report_status" == 0 || "$report_status" == 1 ]] || fail "Offline wheel check did not complete normally (status $report_status)."
        [[ -s "$scratch/$profile-$phase.cid" ]] || fail 'Offline wheel check did not produce a container identity.'
        report_cid="$(<"$scratch/$profile-$phase.cid")"
        [[ "$report_cid" =~ ^[0-9a-f]{64}$ ]] || fail 'Offline wheel check returned an invalid container identity.'
        [[ "$(docker inspect "$report_cid" --format '{{.State.Running}} {{.State.ExitCode}} {{index .Config.Labels "io.numpde.nmrpeak.wheelhouse"}}')" == "false $report_status ${scratch##*.}-$profile-$phase" ]] ||
            fail 'Offline wheel check retirement or ownership could not be confirmed.'
        docker rm "$report_cid" >/dev/null
        engine_active=0
        if (( report_status != 0 )); then
            cat "$scratch/$profile-$phase.log" >&2
            offline_complete=0
            break
        fi
        [[ -s "$scratch/$profile-$phase.json" ]] || fail 'Offline wheel check returned an empty report.'
    done
}
check_offline_selection initial
if (( offline_complete )); then
    printf 'All locked NMRPeak %s wheels are already available in %s; no acquisition is needed.\n' "$target" "$wheelhouse"
    exit 0
fi
printf '%s\n' 'Offline selection did not complete; continuing with the selected acquisition route.' >&2
select_network_interface
network=bridge
proxy_arguments=(--env HTTP_PROXY= --env HTTPS_PROXY= --env http_proxy= --env https_proxy= --env NO_PROXY= --env no_proxy= --env ALL_PROXY= --env all_proxy= --env FTP_PROXY= --env ftp_proxy=)
if [[ "$NETWORK_INTERFACE_SELECTED" != system ]]; then
    network=host
    previous_umask="$(umask)"
    umask 077
    setsid bash -c 'ulimit -f 2048; exec "$@"' bash "$python" "$repo_root/docker/bound_http_proxy.py" \
        --interface="$NETWORK_INTERFACE_SELECTED" --ready-file "$scratch/proxy.port" </dev/null >"$scratch/proxy.log" 2>&1 &
    proxy_pid=$!
    umask "$previous_umask"
    for _ in {1..100}; do
        [[ -s "$scratch/proxy.port" ]] && break
        kill -0 "$proxy_pid" 2>/dev/null || { tail -c 8192 "$scratch/proxy.log" >&2; fail 'Network proxy stopped before wheelhouse acquisition.'; }
        sleep 0.05
    done
    [[ -s "$scratch/proxy.port" ]] || { tail -c 8192 "$scratch/proxy.log" >&2; fail 'Network proxy did not become ready for wheelhouse acquisition.'; }
    proxy_url="http://127.0.0.1:$(<"$scratch/proxy.port")"
    proxy_arguments=(--env "HTTP_PROXY=$proxy_url" --env "HTTPS_PROXY=$proxy_url" --env "http_proxy=$proxy_url" --env "https_proxy=$proxy_url" --env NO_PROXY= --env no_proxy= --env ALL_PROXY= --env all_proxy= --env FTP_PROXY= --env ftp_proxy=)
fi
worker=(--platform linux/amd64 --pull never --network "$network" --read-only --user "$operator_uid:65532"
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 512m
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m "${proxy_arguments[@]}"
    --mount "type=bind,src=$tool,dst=/tool.pyz,readonly"
    --mount "type=bind,src=$wheelhouse,dst=/wheelhouse" --entrypoint python "$base" -I -S /tool.pyz)
run_network_worker() {
    local profile="$1" operation="$2" status cid state running exit_code label extra
    shift 2
    local name="nmrpeak-$profile-$operation-${scratch##*.}"
    local receipt="$scratch/$profile-$operation.cid"
    local expected_label="${scratch##*.}-$profile-$operation"
    engine_active=1
    set +e
    docker run --name "$name" --cidfile "$receipt" \
        --label "io.numpde.nmrpeak.wheelhouse=$expected_label" "$@"
    status=$?
    set -e
    [[ -s "$receipt" ]] || fail "Cannot $operation $profile wheels: worker identity is unavailable (client status $status)."
    cid="$(<"$receipt")"
    [[ "$cid" =~ ^[0-9a-f]{64}$ ]] || fail "Cannot $operation $profile wheels: worker identity is invalid."
    state="$(docker inspect "$cid" --format '{{.State.Running}} {{.State.ExitCode}} {{index .Config.Labels "io.numpde.nmrpeak.wheelhouse"}}')" ||
        fail "Cannot $operation $profile wheels: worker state could not be inspected."
    read -r running exit_code label extra <<<"$state"
    [[ "$running" == false && "$exit_code" =~ ^[0-9]+$ && "$label" == "$expected_label" && -z "$extra" ]] ||
        fail "Cannot $operation $profile wheels: worker retirement or ownership is unconfirmed."
    docker rm "$cid" >/dev/null || fail "Cannot $operation $profile wheels: the stopped worker could not be removed."
    engine_active=0
    if (( status != 0 || exit_code != 0 )); then
        fail "Cannot $operation $profile wheels: client status $status, worker status $exit_code. Inspect the preceding diagnostic; any verified pool files were preserved."
    fi
}
for profile in "${profiles[@]}"; do
    if [[ "$profile" == native ]]; then
        lock="$scratch/native.lock"
        locate_arguments=(--index-url https://pypi.org/simple --source "$native_source")
    elif [[ "$profile" == api ]]; then
        lock="$scratch/api.lock"
        locate_arguments=(--index-url https://pypi.org/simple --source "$native_source")
    elif [[ "$profile" == consumer ]]; then
        lock="$scratch/consumer.lock"
        locate_arguments=(--index-url https://pypi.org/simple)
    elif [[ "$profile" == benchmark ]]; then
        lock="$scratch/benchmark.lock"
        locate_arguments=()
    else
        lock="$scratch/requirements.lock"
        locate_arguments=()
    fi
    run_network_worker "$profile" locate \
        --mount "type=bind,src=$lock,dst=/input/requirements.lock,readonly" \
        "${worker[@]}" locate --lock /input/requirements.lock --pool /wheelhouse "${locate_arguments[@]}" >"$scratch/pylock.$profile.toml"
    [[ -s "$scratch/pylock.$profile.toml" ]] || fail "Cannot locate $profile wheels: the worker returned an empty projection."
    chmod 0644 -- "$scratch/pylock.$profile.toml"
    run_network_worker "$profile" acquire \
        --mount "type=bind,src=$scratch/pylock.$profile.toml,dst=/input/pylock.toml,readonly" \
        --mount "type=bind,src=$lock,dst=/input/source.lock,readonly" \
        "${worker[@]}" acquire --lock /input/pylock.toml --source-lock /input/source.lock --pool /wheelhouse
done
check_offline_selection final
(( offline_complete )) || fail 'Acquisition completed, but the locked wheel selection still fails offline.'
printf 'Acquired the locked NMRPeak %s wheel selection in %s.\n' "$target" "$wheelhouse"
