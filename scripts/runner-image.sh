#!/usr/bin/env bash
set -euo pipefail

readonly runner="${1:-}"
readonly target="${2:-}"

fail() {
    printf '%s\n' "$*" >&2
    exit 2
}

[[ "$target" == cpu-x86_64 ]] || fail "runner image target must be cpu-x86_64: ${target:-<unset>}"

case "$runner" in
    nmrpeak_chf_v1)
        readonly image_repository="numpde/nmrpeak-chf-runner"
        readonly contract_id="nmrpeak.runner_session.chf.v1"
        readonly entrypoint='["/usr/local/bin/python3.12","-P","-m","nmrpeak_provider.native_launcher","nmrpeak_chf_v1","models.nmrpeak_chf_v1.runner.worker"]'
        ;;
    nmrpeak_hf_v1)
        readonly image_repository="numpde/nmrpeak-hf-runner"
        readonly contract_id="nmrpeak.runner_session.hf.v1"
        readonly entrypoint='["/usr/local/bin/python3.12","-P","-m","nmrpeak_provider.native_launcher","nmrpeak_hf_v1","models.nmrpeak_hf_v1.runner.worker"]'
        ;;
    *)
        fail "runner image must select nmrpeak_chf_v1 or nmrpeak_hf_v1: ${runner:-<unset>}"
        ;;
esac

readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
readonly python="${PYTHON:-python3}"
readonly revision="$(git -C "$repo_root" rev-parse --verify HEAD)"
[[ -z "$(git -C "$repo_root" status --short)" ]] ||
    fail 'cannot build a runner from a checkout with uncommitted changes'
source "$repo_root/scripts/network-interface.sh"
select_network_interface
readonly network_interface="$NETWORK_INTERFACE_SELECTED"
require_local_docker
docker() { command docker --host "$NMRPEAK_DOCKER_HOST" "$@"; }
[[ "${NMRPEAK_WHEELHOUSE_INPUT_ORIGIN:-}" == 'command line' && -n "${NMRPEAK_WHEELHOUSE_INPUT:-}" ]] ||
    fail 'set WHEELHOUSE to the existing shared wheelhouse directory on the command line'
[[ -d "$NMRPEAK_WHEELHOUSE_INPUT" && ! -L "$NMRPEAK_WHEELHOUSE_INPUT" ]] ||
    fail 'WHEELHOUSE must name an existing directory, not a symlink'
wheelhouse=''
IFS= read -r -d '' wheelhouse < <(realpath -e -z -- "$NMRPEAK_WHEELHOUSE_INPUT") || fail 'could not resolve WHEELHOUSE'
readonly wheelhouse
[[ "$wheelhouse" != / && "$wheelhouse" != *','* && "$wheelhouse" != *$'\n'* && "$wheelhouse" != *$'\r'* ]] ||
    fail 'WHEELHOUSE must be a selected subdirectory path without commas or line breaks'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" "$python" -m repository_checks.wheelhouse_tool_asset check "$wheelhouse" ||
    fail 'the pinned wheelhouse tool is missing or invalid; run make wheelhouse/tool first'
readonly tool="$wheelhouse/wheelhouse-tool-0.5.7.pyz"

tmp="$(mktemp -d)"
proxy_pid=''
engine_active=0
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$proxy_pid" ]] && (( ! engine_active )); then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    if (( engine_active )); then
        printf 'Build or report retirement is unconfirmed; preserve %s, inspect report CID files and containers nmrpeak-{native,scientific}-report-%s and proxy PID %s before cleanup or retrying.\n' "$tmp" "${tmp##*.}" "${proxy_pid:-none}" >&2
    else
        rm -rf -- "$tmp"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

readonly context="$tmp/context"
install -d -m 0700 -- "$context"
readonly image_input_id="$({
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" \
        "$python" -m repository_checks.nmrpeak_image_inputs \
        "$runner" "$repo_root" "$revision" "$context" \
        --source-allowlist "$tmp/source.json" --source-prefix "$context"
})"
readonly base='docker.io/library/python:3.12.16-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487'
docker image inspect "$base" >/dev/null 2>&1 || fail 'the pinned Python base image must already exist on the selected Docker engine'
git -C "$repo_root" show "$revision:repository_checks/image_wheel_report.py" >"$tmp/report.py"
read -r report_helper_hash _ < <(sha256sum -- "$tmp/report.py")
readonly report_input="$tmp/report-input"
install -d -m 0755 -- "$report_input" "$report_input/dependencies" \
    "$report_input/families/nmrpeak/targets/cpu-x86_64"
chmod 0711 -- "$tmp"
for path in requirements-native.lock dependencies/provider-core-native.lock dependencies/provider-core-source.txt \
    families/nmrpeak/targets/cpu-x86_64/requirements.lock; do
    install -m 0644 -- "$context/$path" "$report_input/$path"
done
for profile in native scientific; do
    if [[ "$profile" == native ]]; then
        locks=(requirements-native.lock dependencies/provider-core-native.lock dependencies/provider-core-source.txt)
    else
        locks=(families/nmrpeak/targets/cpu-x86_64/requirements.lock)
    fi
    lock_arguments=()
    for path in "${locks[@]}"; do
        read -r digest _ < <(sha256sum -- "$context/$path")
        lock_arguments+=(--lock-sha256 "$digest")
    done
    engine_active=1
    docker run --name "nmrpeak-$profile-report-${tmp##*.}" --cidfile "$tmp/$profile.cid" \
        --label "io.numpde.nmrpeak.wheel-report=${tmp##*.}-$profile" \
        --rm --platform linux/amd64 --pull never --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges --pids-limit 64 --memory 512m \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
        --mount "type=bind,src=$report_input,dst=/input,readonly" \
        --mount "type=bind,src=$wheelhouse,dst=/wheelhouse,readonly" \
        --entrypoint python "$base" -I -c "$(<"$tmp/report.py")" \
        --source /input --pool /wheelhouse --profile "$profile" "${lock_arguments[@]}" >"$tmp/$profile.report.json"
    engine_active=0
    "$python" -I -S "$tool" report --report "$tmp/$profile.report.json" --pool /wheelhouse --directory wheels >"$tmp/$profile.selection.json"
done
"$python" -I -c 'import json,sys; print(json.dumps(sum((json.load(open(p)) for p in sys.argv[1:]), [])))' \
    "$tmp/native.selection.json" "$tmp/scientific.selection.json" >"$tmp/selection.json"
readonly selection_id="$("$python" -I -S "$tool" identity --manifest "$tmp/selection.json")"
[[ "$selection_id" =~ ^[0-9a-f]{64}$ ]] || fail 'shared wheelhouse tool returned an invalid selection identity'
read -r build_identity _ < <(printf 'nmrpeak-runner-build-v1\n%s\n%s\n%s\n%s\n%s\n' \
    "$revision" "$image_input_id" "$selection_id" "$report_helper_hash" \
    '6d178096f9196e7f1724503b8fb509658061d5ac203de69719227f2fbd508a96' | sha256sum)
build_network=default
proxy_build_arguments=(--build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= --build-arg http_proxy= --build-arg https_proxy= --build-arg NO_PROXY= --build-arg no_proxy= --build-arg ALL_PROXY= --build-arg all_proxy= --build-arg FTP_PROXY= --build-arg ftp_proxy=)
if [[ "$network_interface" != system ]]; then
    build_network=host
    readonly ready_file="$tmp/proxy.port"
    setsid "$python" "$repo_root/docker/bound_http_proxy.py" \
        --interface="$network_interface" --ready-file "$ready_file" </dev/null >/dev/null 2>&1 &
    proxy_pid=$!
    for _ in {1..100}; do
        [[ -s "$ready_file" ]] && break
        kill -0 "$proxy_pid" 2>/dev/null || fail "Network proxy stopped before the $runner build."
        sleep 0.05
    done
    [[ -s "$ready_file" ]] || fail "Network proxy did not become ready before the $runner build."
    readonly proxy_url="http://127.0.0.1:$(<"$ready_file")"
    proxy_build_arguments=(
        --build-arg "HTTP_PROXY=$proxy_url"
        --build-arg "HTTPS_PROXY=$proxy_url"
        --build-arg "http_proxy=$proxy_url"
        --build-arg "https_proxy=$proxy_url"
        --build-arg "NO_PROXY="
        --build-arg "no_proxy="
        --build-arg "ALL_PROXY="
        --build-arg "all_proxy="
        --build-arg "FTP_PROXY="
        --build-arg "ftp_proxy="
    )
fi
readonly image="$image_repository:${image_input_id#sha256:}"
readonly image_id_file="$tmp/image.id"

engine_active=1
set +e
"$python" -I -S "$tool" stream --pool "$wheelhouse" --source-allowlist "$tmp/source.json" --manifest "$tmp/selection.json" |
docker build --platform linux/amd64 --pull=false --network "$build_network" "${proxy_build_arguments[@]}" \
    --build-arg "SOURCE_REVISION=$revision" --build-arg "IMAGE_INPUT_ID=$image_input_id" \
    --label "io.numpde.nmrpeak.wheelhouse.selection=sha256:$selection_id" \
    --label "io.numpde.nmrpeak.build.input-id=sha256:$build_identity" \
    --iidfile "$image_id_file" --file "models/$runner/runner/Dockerfile.runner" -
pipeline_status=("${PIPESTATUS[@]}")
set -e
[[ "${pipeline_status[0]}" == 0 && "${pipeline_status[1]}" == 0 ]] ||
    fail "runner input stream or build failed (stream ${pipeline_status[0]}, build ${pipeline_status[1]})"
engine_active=0

readonly image_id="$(<"$image_id_file")"
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "Docker returned a malformed $runner image ID: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "$revision" ]] ||
    fail "built $runner image does not record its committed source revision: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.image.input-id"}}')" == "$image_input_id" ]] ||
    fail "built $runner image does not record its exact input identity: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.build.input-id"}}')" == "sha256:$build_identity" ]] ||
    fail "built $runner image does not record its complete build identity: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{.Os}}/{{.Architecture}}')" == linux/amd64 ]] ||
    fail "built $runner image is not Linux amd64: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{.Config.User}}')" == 65532:65532 ]] ||
    fail "built $runner image does not use the fixed non-root identity: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.runner.ref"}}')" == "$runner" ]] ||
    fail "built $runner image does not record its runner reference: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.runner.contract-id"}}')" == "$contract_id" ]] ||
    fail "built $runner image does not record its runner contract: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.runner.target"}}')" == "$target" ]] ||
    fail "built $runner image does not record its target: $image_id"
[[ "$(docker image inspect "$image_id" --format '{{json .Config.Entrypoint}}')" == "$entrypoint" ]] ||
    fail "built $runner image does not use its fixed entrypoint: $image_id"
docker image tag "$image_id" "$image_repository-build:$build_identity"
docker image tag "$image_id" "$image"
printf 'Built %s from %s as %s\n' "$image" "$image_input_id" "$image_id"
