#!/usr/bin/env bash
# Compose executes containers; the enclosing acquisition scope owns their lifetime.
# Caller overlays are trusted, captured files with literal package parameters.
# Only the NMR_ACQ_* topology parameters below are interpolated by this owner.

_acq_compose_command() {
    _acq_compose=(env
        "NMR_ACQ_BOOTSTRAP_IMAGE=$_acq_bootstrap_image"
        "NMR_ACQ_USER=$_acq_scope_uid:$_acq_scope_gid"
        "NMR_ACQ_INTERFACE=${_acq_route:-system}"
        "NMR_ACQ_BUNDLE=$_acq_bundle" "NMR_ACQ_SCRATCH=$_acq_compose_directory"
        "NMR_ACQ_PORT=$_acq_compose_port"
        "${_acq_engine[@]}" compose --project-name "$_acq_compose_project"
        --project-directory "$_acq_compose_directory" --env-file /dev/null
        -f "$_acq_compose_topology" -f "$_acq_compose_directory/caller.yml")
}

_acq_compose_cleanup() {
    [[ $_acq_compose_state != idle ]] || return 0
    # An uncertain removal is retained for recovery, never replayed by EXIT.
    [[ $_acq_compose_state == pending ]] || return 2
    _acq_compose_state=uncertain
    local _acq_cleanup_status=0
    _acq_compose_command
    _acq_native 'acquisition Compose teardown' 30 discard \
        "${_acq_compose[@]}" down --remove-orphans --timeout 10 || _acq_cleanup_status=$?
    "$_acq_native_retired" || return 2
    _acq_capture "$_acq_compose_directory/remaining" ps --all --quiet --no-trunc \
        --filter "label=com.docker.compose.project=$_acq_compose_project" || return 2
    [[ ! -s $_acq_compose_directory/remaining && $_acq_cleanup_status == 0 ]] || {
        _acq_fail "Cannot retire acquisition Compose project $_acq_compose_project; retain its inputs."
        return 2
    }
    _acq_compose_state=idle
}

_acq_compose_wait() {
    local _acq_ready_path=$1
    _acq_native 'acquisition Compose readiness' 30 stdio bash -eu -c '
        for _ in {1..100}; do
            [[ ! -L $1 && -f $1 && -s $1 ]] && exit 0
            sleep 0.1
        done
        printf "Acquisition helper did not publish readiness at %s.\n" "$1" >&2
        exit 2
    ' bash "$_acq_ready_path"
}

# Return a package result only after the one-off container and helpers retire.
# Legacy argv workers remain available for callers that have not migrated.
acquisition_compose_worker() {
    local _acq_compose_call_status=0
    _acq_compose_worker "$@" || _acq_compose_call_status=$?
    if [[ $_acq_compose_call_status != 0 ]]; then _acq_failed=true; fi
    return "$_acq_compose_call_status"
}

_acq_compose_worker() {
    [[ $# == 4 && ${_acq_active:-false} == true && $_acq_compose_state == idle ]] || return 2
    local _acq_mode=$1 _acq_result_name=$2 _acq_caller=$3 _acq_service=$4
    [[ $_acq_mode == online || $_acq_mode == offline ]] || return 2
    [[ $_acq_service == worker-"$_acq_mode" && $_acq_caller == /* && -f $_acq_caller && ! -L $_acq_caller ]] || return 2
    [[ $_acq_result_name =~ ^[A-Za-z_][A-Za-z0-9_]*$ && $_acq_result_name != _acq_* ]] || return 2
    _acq_scope_identity || return 2
    _acq_sequence=$((_acq_sequence + 1))
    _acq_compose_project="acq-${_acq_scratch##*.}-$_acq_sequence"
    _acq_compose_project=${_acq_compose_project,,}
    _acq_compose_directory=$_acq_scratch/compose-$_acq_sequence
    mkdir -m 700 -- "$_acq_compose_directory" || return 2
    cp -- "$_acq_caller" "$_acq_compose_directory/caller.yml" || return 2
    _acq_compose_port=1
    _acq_compose_command
    # Record cleanup intent before the first command that can create containers.
    _acq_compose_state=pending
    local _acq_status=0 _acq_worker_exit= _acq_running= _acq_extra=
    if [[ $_acq_mode == online ]]; then
        [[ -n $_acq_route ]] || { _acq_fail 'Select an acquisition route before online Compose work.'; return 2; }
        _acq_native 'acquisition Compose resolver' 10 stdio python3 -I -S -B "$_acq_bundle/acquisition_tool/dns.py" \
            --interface "$_acq_route" --output "$_acq_compose_directory/resolv.conf" || return 2
        mkdir -m 700 -- "$_acq_compose_directory/gateway-socket" \
            "$_acq_compose_directory/gateway-ready" "$_acq_compose_directory/relay-ready" || return 2
        _acq_native 'acquisition Compose gateway' 30 discard "${_acq_compose[@]}" up -d --no-build --pull never gateway || return 2
        _acq_compose_wait "$_acq_compose_directory/gateway-ready/socket" || return 2
        cmp -s "$_acq_compose_directory/gateway-ready/socket" <(printf '/gateway/proxy.sock\n') \
            && [[ -S $_acq_compose_directory/gateway-socket/proxy.sock ]] || return 2
        _acq_native 'acquisition Compose relay' 30 discard "${_acq_compose[@]}" up -d --no-build --pull never relay || return 2
        _acq_compose_wait "$_acq_compose_directory/relay-ready/port" || return 2
        IFS= read -r _acq_compose_port < "$_acq_compose_directory/relay-ready/port" || return 2
        [[ $_acq_compose_port =~ ^[1-9][0-9]{0,4}$ && $_acq_compose_port -le 65535 ]] \
            && cmp -s "$_acq_compose_directory/relay-ready/port" <(printf '%s\n' "$_acq_compose_port") || return 2
        _acq_compose_command
    fi
    _acq_native 'acquisition Compose worker' untimed stdio "${_acq_compose[@]}" run -T --no-deps --pull never \
        --name "$_acq_compose_project-worker" "$_acq_service" || _acq_status=$?
    "$_acq_native_retired" || return 2
    _acq_capture "$_acq_compose_directory/result" inspect --format '{{.State.Running}} {{.State.ExitCode}}' \
        "$_acq_compose_project-worker" || return 2
    read -r _acq_running _acq_worker_exit _acq_extra < "$_acq_compose_directory/result" || return 2
    [[ $_acq_running == false && $_acq_worker_exit =~ ^[0-9]+$ && -z $_acq_extra && $_acq_worker_exit == "$_acq_status" ]] || {
        _acq_fail 'Cannot admit acquisition Compose worker result: client and terminal state disagree.'
        return 2
    }
    _acq_compose_cleanup || {
        _acq_fail "Acquisition Compose worker completed with status $_acq_worker_exit; cleanup is unresolved."
        return 2
    }
    printf -v "$_acq_result_name" '%s' "$_acq_worker_exit"
}
