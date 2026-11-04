#!/usr/bin/env bash
# Shared acquisition lifecycle. Sourcing defines functions without changing traps.
_acq_bundle_path() { (cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P); }

_acq_container_id_matches_receipt() {
    local identifier=$1 receipt=$2
    # Native effects require one exact ID, never a name from create output.
    # Startup also requires agreement with create output. During cancellation,
    # the private receipt can precede stdout assignment and still owns cleanup.
    # Nonempty disagreeing identities never authorize removal.
    # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/selected-image-build.py#L343-L370
    [[ "$identifier" =~ ^[0-9a-f]{64}$ && -f "$receipt" && ! -L "$receipt" ]] && {
        # Compare bytes: shell substitution discards NULs and trailing newlines.
        cmp -s "$receipt" <(printf '%s' "$identifier") ||
            cmp -s "$receipt" <(printf '%s\n' "$identifier")
    }
}

_acq_private_receipt_below_limit() {
    # The fixed metadata wrapper caps EACH file at 4096 bytes. Reaching the cap
    # cannot prove complete output, even if the native client exits zero.
    local size
    if [[ ! -f "$1" || -L "$1" ]]; then return 1; fi
    if size=$(stat -c '%s' -- "$1" 2>/dev/null); then :; else return 1; fi
    # Bound arithmetic to the admitted decimal width; missing or malformed
    # metadata must not become numeric zero or overflow into an accepted size.
    [[ "$size" =~ ^(0|[1-9][0-9]{0,3})$ && "$size" -lt 4096 ]]
}

_acq_cancellation_stdout_allows_identity() {
    local identifier=$1 output=$2
    # Cancellation can precede create stdout, but cannot erase contradictory
    # bytes already published there. CID-only recovery requires absent or
    # provably empty stdout; every nonempty receipt must agree exactly.
    if [[ ! -e "$output" && ! -L "$output" ]]; then return 0; fi
    if [[ ! -f "$output" || -L "$output" ]]; then return 1; fi
    if cmp -s -- "$output" /dev/null; then return 0; fi
    _acq_container_id_matches_receipt "$identifier" "$output"
}


_acq_fail() {
    _acq_failed=true
    printf -v _acq_diagnostic '%s%s\n' "$_acq_diagnostic" "$*"
    return 2
}

_acq_native() {
    local _acq_call_status=0
    local inference_retirement_confirmed=$_acq_native_retired inference_cancellation_status=0 inference_diagnostic= inference_result=
    if ! "$inference_retirement_confirmed" || "$_acq_identity_unsettled"; then
        _acq_fail 'Cannot dispatch another acquisition command: prior retirement is unconfirmed.'
        return 2
    fi
    if inference_native "$@"; then :; else _acq_call_status=$?; fi
    _acq_diagnostic+=$inference_diagnostic
    _acq_native_retired=$inference_retirement_confirmed
    if [[ $inference_cancellation_status != 0 ]]; then
        _acq_cancelled=$inference_cancellation_status
        exit "$_acq_cancelled"
    fi
    if ! "$inference_retirement_confirmed"; then
        _acq_failed=true
        [[ $_acq_call_status != 0 ]] || _acq_call_status=2
    fi
    return "$_acq_call_status"
}

_acq_capture() {
    local _acq_capture_file=$1 _acq_capture_status=0
    shift
    _acq_native 'acquisition metadata' 30 stdio bash -eu -c '
        file=$1; shift; umask 077; ulimit -f 4
        exec "$@" > "$file" 2> "$file.stderr"
    ' bash "$_acq_capture_file" "${_acq_engine[@]}" "$@" || _acq_capture_status=$?
    [[ $_acq_capture_status == 0 ]] || return "$_acq_capture_status"
    _acq_private_receipt_below_limit "$_acq_capture_file"         && _acq_private_receipt_below_limit "$_acq_capture_file.stderr"
}

# The helper writes host-owner mapping only after its qualified probe retires.
acquisition_map_owner() {
    local _acq_uid_name=$1 _acq_gid_name=$2 _acq_map_image=$3
    local _acq_host_uid=${4:-$(id -u)} _acq_host_gid=${5:-$(id -g)}
    local _acq_map_endpoint _acq_map_bundle _acq_map_directory _acq_map_status=0 _acq_map_uid= _acq_map_gid= _acq_map_diagnostic=
    local inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic=
    _acq_map_endpoint=${_acq_endpoint:-${NMR_SELECTED_DOCKER_ENDPOINT:-}}
    [[ -n $_acq_map_endpoint ]] || { printf '%s\n' 'Identity mapping requires an explicitly selected endpoint.' >&2; return 2; }
    _acq_map_bundle=$(_acq_bundle_path) || return 2
    source "$_acq_map_bundle/tools/inference_native.sh"
    _acq_map_directory=$(umask 077; mktemp -d /tmp/acquisition-map.XXXXXXXX) || return 2
    if controller_native 'acquisition identity probe' untimed stdio bash -eu -c '
        output=$1; shift; umask 077; exec "$@" > "$output" 2> "$output.stderr"
    ' bash "$_acq_map_directory/output" python3 -I -S -B "$_acq_map_bundle/acquisition_tool/identity.py"         --endpoint "$_acq_map_endpoint" --image "$_acq_map_image"         --host-uid "$_acq_host_uid" --host-gid "$_acq_host_gid"         --retired-receipt "$_acq_map_directory/retired"; then :; else _acq_map_status=$?; fi
    _acq_map_diagnostic=$inference_diagnostic
    if [[ -s $_acq_map_directory/output.stderr ]]; then
        _acq_map_diagnostic+=$(<"$_acq_map_directory/output.stderr")
        _acq_map_diagnostic+=$'\n'
    fi
    if [[ ${_acq_active:-false} == true ]]; then _acq_diagnostic+=$_acq_map_diagnostic; fi
    if ! "$inference_retirement_confirmed" || ! cmp -s "$_acq_map_directory/retired" <(printf 'retired\n'); then
        if [[ ${_acq_active:-false} == true ]]; then _acq_identity_unsettled=true; _acq_failed=true; fi
        if [[ ${_acq_active:-false} == true ]]; then
            printf -v _acq_diagnostic '%sCannot confirm identity probe retirement; retained evidence at %q.\n' "$_acq_diagnostic" "$_acq_map_directory"
        else
            printf '%s' "$_acq_map_diagnostic" >&2
            printf 'Cannot confirm identity probe retirement; retained evidence at %q.\n' "$_acq_map_directory" >&2
        fi
        if [[ $inference_cancellation_status != 0 ]]; then
            if [[ ${_acq_active:-false} == true ]]; then
                _acq_cancelled=$inference_cancellation_status; exit "$_acq_cancelled"
            fi
            return "$inference_cancellation_status"
        fi
        return 2
    fi
    if [[ $_acq_map_status == 0 ]]; then
        IFS=' ' read -r _acq_map_uid _acq_map_gid < "$_acq_map_directory/output" || _acq_map_status=2
        if [[ ! $_acq_map_uid =~ ^[0-9]+$ || ! $_acq_map_gid =~ ^[0-9]+$ ]]             || ! cmp -s "$_acq_map_directory/output" <(printf '%s %s\n' "$_acq_map_uid" "$_acq_map_gid"); then _acq_map_status=2; fi
    fi
    rm -rf -- "$_acq_map_directory" || return 2
    if [[ ${_acq_active:-false} != true ]]; then printf '%s' "$_acq_map_diagnostic" >&2; fi
    if [[ ${_acq_active:-false} == true && ( $_acq_map_status == 129 || $_acq_map_status == 130 || $_acq_map_status == 143 ) ]]; then
        _acq_cancelled=$_acq_map_status; exit "$_acq_map_status"
    fi
    [[ $_acq_map_status == 0 ]] || return "$_acq_map_status"
    printf -v "$_acq_uid_name" '%s' "$_acq_map_uid"
    printf -v "$_acq_gid_name" '%s' "$_acq_map_gid"
}

_acq_create() {
    local _acq_role=$1 _acq_image=$2 _acq_options_name=$3 _acq_arguments_name=$4 _acq_create_status=0 _acq_candidate=
    local -n _acq_options_ref=$_acq_options_name _acq_arguments_ref=$_acq_arguments_name
    local _acq_role_directory=$_acq_scratch/$_acq_role
    mkdir -m 700 -- "$_acq_role_directory" || return 2
    _acq_roles+=("$_acq_role")
    _acq_attempted[$_acq_role]=true
    _acq_native "acquisition $_acq_role creation" 30 stdio bash -eu -c '
        directory=$1; shift; umask 077; ulimit -f 4
        exec "$@" > "$directory/stdout" 2> "$directory/stderr"
    ' bash "$_acq_role_directory" "${_acq_engine[@]}" create --cidfile "$_acq_role_directory/cid"         "${_acq_options_ref[@]}" "$_acq_image" "${_acq_arguments_ref[@]}" || _acq_create_status=$?
    if _acq_private_receipt_below_limit "$_acq_role_directory/stdout"         && IFS= read -r -N 64 _acq_candidate < "$_acq_role_directory/stdout"         && _acq_container_id_matches_receipt "$_acq_candidate" "$_acq_role_directory/stdout"         && _acq_container_id_matches_receipt "$_acq_candidate" "$_acq_role_directory/cid"; then
        _acq_cids[$_acq_role]=$_acq_candidate
    fi
    if [[ $_acq_create_status != 0 ]]; then
        _acq_fail "Cannot create $_acq_role: creation did not complete (status $_acq_create_status)."
        return "$_acq_create_status"
    fi
    [[ -n ${_acq_cids[$_acq_role]:-} ]] && _acq_private_receipt_below_limit "$_acq_role_directory/stderr"         || { _acq_fail "Cannot admit $_acq_role: create output and CID receipt must agree."; return 2; }
}

_acq_remove() {
    local _acq_remove_role=$1 _acq_remove_cid=${_acq_cids[$1]:-} _acq_remove_directory=$_acq_scratch/$1
    [[ ${_acq_removed[$_acq_remove_role]:-false} != true ]] || return 0
    if [[ -z $_acq_remove_cid && $_acq_cancelled != 0 ]]; then
        if IFS= read -r -N 64 _acq_remove_cid < "$_acq_remove_directory/cid"             && _acq_container_id_matches_receipt "$_acq_remove_cid" "$_acq_remove_directory/cid"             && _acq_cancellation_stdout_allows_identity "$_acq_remove_cid" "$_acq_remove_directory/stdout"; then
            _acq_cids[$_acq_remove_role]=$_acq_remove_cid
        fi
    fi
    _acq_container_id_matches_receipt "$_acq_remove_cid" "$_acq_remove_directory/cid" || return 2
    _acq_native "acquisition $_acq_remove_role removal" 30 discard "${_acq_engine[@]}" rm --force "$_acq_remove_cid" || return "$?"
    _acq_capture "$_acq_remove_directory/absence" ps --all --quiet --no-trunc --filter "id=$_acq_remove_cid" || return "$?"
    [[ ! -s $_acq_remove_directory/absence ]] || return 2
    _acq_removed[$_acq_remove_role]=true
}

_acq_ready() {
    local _acq_ready_role=$1 _acq_ready_file=$2 _acq_ready_cid=${_acq_cids[$1]} _acq_ready_status=0
    _acq_capture "$_acq_scratch/$_acq_ready_role/start" start "$_acq_ready_cid" || return "$?"
    _acq_native "acquisition $_acq_ready_role readiness" 30 stdio bash -eu -c '
        ready=$1 directory=$2 cid=$3; shift 3; umask 077; ulimit -f 4
        for _ in {1..100}; do
            if [[ -f "$ready" && ! -L "$ready" && -s "$ready" ]]; then exit 0; fi
            "$@" inspect --format "{{.State.Running}}" "$cid" > "$directory/state" 2> "$directory/state.stderr" || exit 2
            cmp -s "$directory/state" <(printf "true\n") || exit 2
            sleep 0.05
        done
        exit 2
    ' bash "$_acq_ready_file" "$_acq_scratch/$_acq_ready_role" "$_acq_ready_cid" "${_acq_engine[@]}" || _acq_ready_status=$?
    [[ $_acq_ready_status == 0 ]] || { _acq_fail "Cannot start acquisition: $_acq_ready_role readiness failed."; return "$_acq_ready_status"; }
    _acq_private_receipt_below_limit "$_acq_ready_file"
}

acquisition_set_route() {
    [[ ${_acq_active:-false} == true && $# == 1 && -n $1 ]] || return 2
    [[ -z $_acq_route || $_acq_route == "$1" ]] || { _acq_fail 'Cannot change route in an active acquisition scope.'; return 2; }
    _acq_route=$1
}

_acq_scope_identity() {
    [[ -z $_acq_scope_uid ]] || return 0
    # Mapping belongs to the selected engine's default user namespace. All
    # scope containers use that policy; application images need no interpreter.
    acquisition_map_owner _acq_scope_uid _acq_scope_gid "$_acq_bootstrap_image"
}

_acq_start_route() {
    [[ -z ${_acq_cids[relay]:-} ]] || return 0
    [[ -n $_acq_route ]] || { _acq_fail 'Select an explicit acquisition route before online work.'; return 2; }
    _acq_scope_identity || { _acq_failed=true; return 2; }
    _acq_native 'acquisition resolver capture' 10 stdio python3 -I -S -B "$_acq_bundle/acquisition_tool/dns.py" \
        --interface "$_acq_route" --output "$_acq_scratch/resolv.conf" || { _acq_failed=true; return 2; }
    mkdir -m 700 -- "$_acq_scratch/gateway-socket" "$_acq_scratch/gateway-ready" "$_acq_scratch/relay-ready" || return 2
    local -a _acq_base_options=(--pull never --read-only --user "$_acq_scope_uid:$_acq_scope_gid"
        --cap-drop ALL --security-opt no-new-privileges:true --cpus 1 --memory 128m --memory-swap 128m --pids-limit 64
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m --env PROVIDER_CORE_IN_CONTAINER=1
        --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/bootstrap
        --mount "type=bind,src=$_acq_bundle/provider_core,dst=/bootstrap/provider_core,readonly" --entrypoint python3)
    local -a _acq_gateway_options=("${_acq_base_options[@]}" --network host
        --mount "type=bind,src=$_acq_scratch/gateway-socket,dst=/gateway"
        --mount "type=bind,src=$_acq_scratch/gateway-ready,dst=/ready")
    _acq_gateway_options+=(--mount "type=bind,src=$_acq_scratch/resolv.conf,dst=/gateway-resolv.conf,readonly")
    local -a _acq_gateway_args=(-P -B -m provider_core.bound_http_proxy --interface "$_acq_route"
        --resolver-file /gateway-resolv.conf
        --unix-socket /gateway/proxy.sock --ready-file /ready/socket)
    _acq_create gateway "$_acq_bootstrap_image" _acq_gateway_options _acq_gateway_args || return "$?"
    _acq_ready gateway "$_acq_scratch/gateway-ready/socket" || return "$?"
    cmp -s "$_acq_scratch/gateway-ready/socket" <(printf '/gateway/proxy.sock\n')         && [[ -S $_acq_scratch/gateway-socket/proxy.sock ]] || { _acq_fail 'Gateway Unix readiness is invalid.'; return 2; }
    local -a _acq_relay_options=("${_acq_base_options[@]}" --network none
        --mount "type=bind,src=$_acq_scratch/gateway-socket,dst=/gateway,readonly"
        --mount "type=bind,src=$_acq_scratch/relay-ready,dst=/ready")
    local -a _acq_relay_args=(-P -B -m provider_core.bound_http_proxy --relay-socket /gateway/proxy.sock --port 0 --ready-file /ready/port)
    _acq_create relay "$_acq_bootstrap_image" _acq_relay_options _acq_relay_args || return "$?"
    _acq_ready relay "$_acq_scratch/relay-ready/port" || return "$?"
    IFS= read -r _acq_port < "$_acq_scratch/relay-ready/port" || return 2
    [[ $_acq_port =~ ^[1-9][0-9]{0,4}$ && $_acq_port -le 65535 ]]         && cmp -s "$_acq_scratch/relay-ready/port" <(printf '%s\n' "$_acq_port")         || { _acq_fail 'Relay readiness is not one port.'; return 2; }
    _acq_capture "$_acq_scratch/relay/mode" inspect --format '{{.HostConfig.NetworkMode}} {{.State.Running}}' "${_acq_cids[relay]}" || return "$?"
    cmp -s "$_acq_scratch/relay/mode" <(printf 'none true\n') || return 2
}

acquisition_worker() {
    local _acq_budget=untimed
    if [[ ${1:-} == --timeout ]]; then _acq_budget=$2; shift 2; fi
    [[ $# == 5 && ${_acq_active:-false} == true ]] || return 2
    local _acq_mode=$1 _acq_result_name=$2 _acq_image=$3 _acq_create_name=$4 _acq_app_name=$5
    local _acq_token _acq_attach_status=0 _acq_running _acq_exit _acq_extra
    local -n _acq_create_ref=$_acq_create_name _acq_app_ref=$_acq_app_name
    [[ $_acq_result_name =~ ^[A-Za-z_][A-Za-z0-9_]*$ && $_acq_result_name != _acq_* ]] || return 2
    [[ $_acq_mode == offline || $_acq_mode == online ]] || return 2
    for _acq_token in "${_acq_create_ref[@]}"; do
        case "$_acq_token" in
            --network|--network=*|--net|--net=*|--cidfile|--cidfile=*|--user|--user=*|--userns|--userns=*)
                _acq_fail 'Worker creation network, user namespace, user and CID belong to the acquisition owner.'; return 2 ;;
        esac
    done
    _acq_scope_identity || { _acq_failed=true; return 2; }
    local -a _acq_worker_options=(--pull never --user "$_acq_scope_uid:$_acq_scope_gid" "${_acq_create_ref[@]}")
    if [[ $_acq_mode == online ]]; then
        _acq_start_route || { _acq_failed=true; return 2; }
        _acq_worker_options+=(--network "container:${_acq_cids[relay]}"
            --env "HTTP_PROXY=http://127.0.0.1:$_acq_port" --env "HTTPS_PROXY=http://127.0.0.1:$_acq_port"
            --env "http_proxy=http://127.0.0.1:$_acq_port" --env "https_proxy=http://127.0.0.1:$_acq_port"
            --env NO_PROXY= --env no_proxy= --env ALL_PROXY= --env all_proxy=)
    else
        _acq_worker_options+=(--network none)
    fi
    _acq_sequence=$((_acq_sequence + 1))
    local _acq_worker_role=worker-$_acq_sequence
    _acq_create "$_acq_worker_role" "$_acq_image" _acq_worker_options "$_acq_app_name" || { _acq_failed=true; return 2; }
    _acq_capture "$_acq_scratch/$_acq_worker_role/mode" inspect --format '{{.HostConfig.NetworkMode}}' "${_acq_cids[$_acq_worker_role]}" || { _acq_failed=true; return 2; }
    local _acq_expected_mode=none
    [[ $_acq_mode != online ]] || _acq_expected_mode=container:${_acq_cids[relay]}
    cmp -s "$_acq_scratch/$_acq_worker_role/mode" <(printf '%s\n' "$_acq_expected_mode") || { _acq_failed=true; return 2; }
    _acq_native 'acquisition worker attachment' "$_acq_budget" stdio "${_acq_engine[@]}" start --attach "${_acq_cids[$_acq_worker_role]}" || _acq_attach_status=$?
    "$_acq_native_retired" || { _acq_failed=true; return 2; }
    _acq_capture "$_acq_scratch/$_acq_worker_role/state" inspect --format '{{.State.Running}} {{.State.ExitCode}}' "${_acq_cids[$_acq_worker_role]}" || { _acq_failed=true; return 2; }
    read -r _acq_running _acq_exit _acq_extra < "$_acq_scratch/$_acq_worker_role/state" || return 2
    if [[ $_acq_running != false || ! $_acq_exit =~ ^[0-9]+$ || -n $_acq_extra || $_acq_exit != "$_acq_attach_status" ]]; then
        _acq_failed=true; return 2
    fi
    cmp -s "$_acq_scratch/$_acq_worker_role/state" <(printf '%s %s\n' "$_acq_running" "$_acq_exit") \
        || { _acq_failed=true; return 2; }
    _acq_remove "$_acq_worker_role" || { _acq_failed=true; return 2; }
    printf -v "$_acq_result_name" '%s' "$_acq_exit"
}

_acq_restore_traps() {
    trap - EXIT HUP INT TERM
    [[ -z $_acq_saved_hup ]] || eval "$_acq_saved_hup"
    [[ -z $_acq_saved_int ]] || eval "$_acq_saved_int"
    [[ -z $_acq_saved_term ]] || eval "$_acq_saved_term"
    [[ -z $_acq_saved_exit ]] || eval "$_acq_saved_exit"
}

_acq_finish() {
    local _acq_finish_status=$1 _acq_finish_role _acq_index _acq_retired=true
    trap '' HUP INT TERM
    if ! "$_acq_native_retired" || "$_acq_identity_unsettled"; then _acq_retired=false; fi
    if "$_acq_retired"; then
        for ((_acq_index=${#_acq_roles[@]}-1; _acq_index>=0; _acq_index--)); do
            _acq_finish_role=${_acq_roles[$_acq_index]}
            if ! _acq_remove "$_acq_finish_role"; then _acq_retired=false; break; fi
        done
    fi
    if "$_acq_retired"; then
        # This receipt authorizes releasing caller inputs: every process and
        # container reader is retired. Disposable file cleanup can still fail.
        if (umask 077; set -o noclobber; printf 'retired\n' > "$_acq_retired_receipt"); then :; else
            [[ $_acq_finish_status != 0 ]] || _acq_finish_status=2
        fi
        _acq_cleanup_client || { [[ $_acq_finish_status != 0 ]] || _acq_finish_status=2; }
        rm -rf -- "$_acq_scratch" || { [[ $_acq_finish_status != 0 ]] || _acq_finish_status=2; }
    else
        printf -v _acq_diagnostic '%sCannot confirm acquisition retirement; retained operation %q.\n' "$_acq_diagnostic" "$_acq_scratch"
        [[ $_acq_finish_status != 0 ]] || _acq_finish_status=2
    fi
    if "$_acq_failed" && [[ $_acq_finish_status == 0 ]]; then _acq_finish_status=2; fi
    _acq_final_status=$_acq_finish_status
    _acq_active=false
}

_acq_report() {
    local inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic= inference_result=
    [[ -n $_acq_diagnostic && $_acq_cancelled == 0 ]] || return 0
    # Reporting borrows only this message. Its separate native outcome cannot
    # overwrite the scope's engine/client uncertainty or block caller cleanup.
    if inference_native 'acquisition diagnostic reporting' 2 stdio \
        bash -c 'printf "%s" "$1" >&2' bash "$_acq_diagnostic"; then :; else :; fi
}

_acq_scope_exit() {
    local _acq_exit_status=$1
    # A forked callback shell may inherit EXIT, but owns no parent containers.
    [[ $BASHPID == "$_acq_owner_pid" ]] || return "$_acq_exit_status"
    trap - EXIT
    _acq_finish "$_acq_exit_status"
    _acq_restore_traps
    _acq_report
    if [[ -n $_acq_saved_exit ]]; then
        local _acq_old_exit_code
        eval "set -- ${_acq_saved_exit#trap -- }"
        _acq_old_exit_code=$1
        if (exit "$_acq_final_status"); then eval "$_acq_old_exit_code"; else eval "$_acq_old_exit_code"; fi
    fi
    exit "$_acq_final_status"
}

# Bind the existing backends' variables at their boundary, so callback locals
# such as engine cannot replace this scope's selected endpoint or cleanup facts.
_acq_prepare_client() {
    local docker_client_directory= docker_client_diagnostic= NMR_SELECTED_DOCKER_ENDPOINT=$_acq_endpoint
    local -a engine=()
    local _acq_prepare_status=0
    docker_client_prepare run || _acq_prepare_status=$?
    _acq_client_directory=$docker_client_directory
    _acq_engine=("${engine[@]}")
    _acq_diagnostic+=$docker_client_diagnostic
    return "$_acq_prepare_status"
}

_acq_cleanup_client() {
    local docker_client_directory=$_acq_client_directory _acq_client_status=0
    docker_client_cleanup || _acq_client_status=$?
    _acq_client_directory=$docker_client_directory
    return "$_acq_client_status"
}

acquisition_scope() {
    [[ ${_acq_active:-false} != true ]] || return 2
    # Bash can unwind function locals before EXIT under errexit. Keep this
    # process's cleanup authority alive until its owned trap has finished.
    declare -g _acq_endpoint= _acq_bootstrap_image= _acq_retired_receipt=
    while [[ $# != 0 ]]; do
        case "$1" in
            --endpoint) _acq_endpoint=$2; shift 2 ;;
            --bootstrap-image) _acq_bootstrap_image=$2; shift 2 ;;
            --retired-receipt) _acq_retired_receipt=$2; shift 2 ;;
            --) shift; break ;;
            *) printf '%s\n' 'Unknown acquisition scope argument.' >&2; return 2 ;;
        esac
    done
    [[ -n $_acq_endpoint && -n $_acq_bootstrap_image && $# != 0 ]] || return 2
    [[ -z $_acq_retired_receipt || ( ! -e $_acq_retired_receipt && ! -L $_acq_retired_receipt ) ]] || return 2
    declare -g _acq_saved_exit= _acq_saved_hup= _acq_saved_int= _acq_saved_term=
    _acq_saved_exit=$(trap -p EXIT); _acq_saved_hup=$(trap -p HUP); _acq_saved_int=$(trap -p INT); _acq_saved_term=$(trap -p TERM)
    declare -g _acq_bundle= _acq_scratch= _acq_route= _acq_port= _acq_sequence=0 _acq_active=false _acq_scope_uid= _acq_scope_gid=
    declare -g _acq_failed=false _acq_identity_unsettled=false _acq_cancelled=0 _acq_diagnostic= _acq_final_status=0
    declare -g _acq_native_retired=true _acq_client_directory= _acq_owner_pid=$BASHPID
    declare -ga _acq_engine=() _acq_roles=()
    declare -gA _acq_cids=() _acq_attempted=() _acq_removed=()
    _acq_bundle=$(_acq_bundle_path) || return 2
    source "$_acq_bundle/tools/inference_native.sh"
    source "$_acq_bundle/tools/docker_client.sh"
    _acq_scratch=$(umask 077; mktemp -d /tmp/acquisition.XXXXXXXX) || return 2
    [[ -n $_acq_retired_receipt ]] || _acq_retired_receipt=$_acq_scratch/retired
    _acq_active=true
    trap '_acq_scope_exit "$?"' EXIT
    trap '_acq_cancelled=129; exit 129' HUP
    trap '_acq_cancelled=130; exit 130' INT
    trap '_acq_cancelled=143; exit 143' TERM
    if ! _acq_prepare_client; then
        _acq_failed=true
        _acq_finish 2
        _acq_restore_traps
        _acq_report
        return "$_acq_final_status"
    fi
    "$@"
    local _acq_callback_status=$?
    trap - EXIT
    _acq_finish "$_acq_callback_status"
    _acq_restore_traps
    _acq_report
    return "$_acq_final_status"
}
