#!/usr/bin/env bash
set -euo pipefail

readonly action="${1:-}"
readonly target="${2:-}"

fail() {
    printf '%s\n' "$*" >&2
    exit 2
}

[[ "$action" == stage || "$action" == check || "$action" == apply ]] ||
    fail 'runner lock action must be stage, check, or apply'
[[ "$target" == cpu-x86_64 ]] ||
    fail "runner lock target must be cpu-x86_64: ${target:-<unset>}"

readonly repo_root="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/..")"
readonly family_root="$repo_root/families/nmrpeak"
readonly target_root="$family_root/targets/$target"
readonly intent="$family_root/pyproject.toml"
readonly lock="$target_root/requirements.lock"
readonly renderer="$target_root/Dockerfile.lock"
readonly renderer_ignore="$target_root/Dockerfile.lock.dockerignore"
readonly compiler="$repo_root/containers/runner-lock/seed-and-compile.sh"
readonly python="${PYTHON:-python3}"
read -r repo_digest _ < <(printf '%s' "$repo_root" | sha256sum)
readonly candidate_parent="$(realpath -e -- "${TMPDIR:-/tmp}")"
[[ "$candidate_parent" != "$repo_root" && "$candidate_parent" != "$repo_root/"* ]] ||
    fail "runner lock candidates must be staged outside the checkout: $candidate_parent"
readonly candidate_root="$candidate_parent/nmrpeak-lock-$(id -u)-${repo_digest:0:12}"
readonly candidate="$candidate_root/$target.requirements.lock"
readonly canonical_command="make runner/lock/stage TARGET=$target"

require_regular_file() {
    [[ -f "$1" && ! -L "$1" ]] || fail "runner lock input must be a regular non-symlink file: $1"
}

require_regular_file "$intent"
require_regular_file "$lock"

check_file() {
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root" \
        "$python" -m repository_checks.nmrpeak_lock "$repo_root" "$target" "$1"
}

if [[ "$action" == check ]]; then
    check_file "$lock"
    exit 0
fi

if [[ -e "$candidate_root" ]]; then
    [[ -d "$candidate_root" && ! -L "$candidate_root" && -O "$candidate_root" ]] ||
        fail "runner lock candidate directory is not an owned non-symlink directory: $candidate_root"
else
    install -d -m 0700 -- "$candidate_root"
fi
if [[ -e "$candidate" || -L "$candidate" ]]; then
    require_regular_file "$candidate"
fi
exec 9<"$candidate_root"
flock -x -w 600 9 || fail "runner lock operation is busy after 600 seconds: $candidate_root"

if [[ "$action" == apply ]]; then
    require_regular_file "$candidate"
    readonly intent_digest="$(sha256sum -- "$intent")"
    staged_lock="$(mktemp "$target_root/.requirements.lock.tmp.XXXXXX")"
    trap 'rm -f -- "$staged_lock"' EXIT
    install -m 0644 -- "$candidate" "$staged_lock"
    check_file "$staged_lock"
    [[ "$(sha256sum -- "$intent")" == "$intent_digest" ]] ||
        fail "family dependency intent changed while applying the $target lock: $intent"
    mv -f -- "$staged_lock" "$lock"
    staged_lock=''
    trap - EXIT
    exit 0
fi

require_regular_file "$renderer"
require_regular_file "$renderer_ignore"
require_regular_file "$compiler"
readonly wifi_interface="${NMRPEAK_WIFI_INTERFACE:-wlp1s0}"
[[ -d "/sys/class/net/$wifi_interface" ]] ||
    fail "cannot stage the $target lock because the Wi-Fi interface does not exist: $wifi_interface"

tmp="$(mktemp -d)"
proxy_pid=''
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$proxy_pid" ]]; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    rm -rf -- "$tmp"
    exit "$status"
}
trap cleanup EXIT

readonly context="$tmp/context"
install -d -- "$context/containers/runner-lock" "$context/families/nmrpeak/targets/$target"
cp -- "$compiler" "$context/containers/runner-lock/seed-and-compile.sh"
cp -- "$renderer" "$context/families/nmrpeak/targets/$target/Dockerfile.lock"
cp -- "$renderer_ignore" "$context/families/nmrpeak/targets/$target/Dockerfile.lock.dockerignore"
cp -- "$intent" "$tmp/pyproject.toml"
cp -- "$lock" "$tmp/requirements.seed"
readonly input_digests="$(sha256sum -- "$intent" "$lock" "$renderer" "$renderer_ignore" "$compiler")"

readonly ready_file="$tmp/proxy.port"
"$python" "$repo_root/docker/bound_http_proxy.py" \
    --interface "$wifi_interface" --ready-file "$ready_file" &
proxy_pid=$!
for _ in {1..100}; do
    [[ -s "$ready_file" ]] && break
    kill -0 "$proxy_pid" 2>/dev/null || fail "Wi-Fi proxy stopped before staging the $target lock."
    sleep 0.05
done
[[ -s "$ready_file" ]] || fail "Wi-Fi proxy did not become ready before staging the $target lock."
readonly proxy_url="http://127.0.0.1:$(<"$ready_file")"
readonly image="nmrpeak-lock-renderer:${repo_digest:0:12}-$target"

docker build --network host \
    --build-arg "HTTP_PROXY=$proxy_url" --build-arg "HTTPS_PROXY=$proxy_url" \
    --tag "$image" --file "$context/families/nmrpeak/targets/$target/Dockerfile.lock" \
    "$context" >&2

readonly rendered="$tmp/requirements.rendered"
set +e
docker run --rm --network host --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 64 --cpus 1 --memory 2g \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=1g,mode=1777 \
    --tmpfs /out:rw,noexec,nosuid,nodev,size=40m,mode=1777 \
    --user "$(id -u):$(id -g)" \
    --env "HTTP_PROXY=$proxy_url" --env "HTTPS_PROXY=$proxy_url" \
    --env "http_proxy=$proxy_url" --env "https_proxy=$proxy_url" \
    --mount "type=bind,src=$tmp/pyproject.toml,dst=/work/pyproject.toml,readonly" \
    --mount "type=bind,src=$tmp/requirements.seed,dst=/seed/requirements.lock,readonly" \
    --workdir /work "$image" pyproject.toml --group "$target" \
    --custom-compile-command "$canonical_command" --output-file /out/requirements.lock |
    head -c $((16 * 1024 * 1024 + 1)) >"$rendered"
pipeline_status=("${PIPESTATUS[@]}")
set -e
[[ "${pipeline_status[0]}" == 0 ]] || exit "${pipeline_status[0]}"
[[ "${pipeline_status[1]}" == 0 ]] || fail "cannot capture the staged $target lock within 16 MiB"
check_file "$rendered"
[[ "$(sha256sum -- "$intent" "$lock" "$renderer" "$renderer_ignore" "$compiler")" == "$input_digests" ]] ||
    fail "runner lock inputs changed while staging the $target candidate."
candidate_stage="$(mktemp "$candidate_root/.requirements.lock.tmp.XXXXXX")"
install -m 0600 -- "$rendered" "$candidate_stage"
mv -f -- "$candidate_stage" "$candidate"
printf 'Staged %s lock candidate at %s\n' "$target" "$candidate"
