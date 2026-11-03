#!/usr/bin/env bash
# Engine-client deadlines do not establish cancellation of engine-side work.
# Capped private evidence and held lifetime follow Magnet's native owner policy:
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/selected-image-build.py#L455-L521
# The internal untimed budget preserves the same held lifetime without imposing
# a scientific deadline. Numeric budgets retain the existing timeout policy.
# inference_retirement_confirmed covers this ONE invocation's local processes,
# not engine-side work or evidence removal. Callers initialize it before cleanup
# traps and read it directly. After false, retain resources and stop: another
# call would replace the fact while the earlier unconfirmed group may survive.
# Internal result=stdio inherits stdout/stderr directly, without a capture log,
# FIFO or drainer. It keeps the same held anchor and private control receipts.
# Native stdin remains /dev/null. Baseline Docker uses no -i/-t and its supported
# image has OpenStdin=false; this is not an all-file-descriptor library API.
# Unknown stdio retirement can leave public pipe writers alive; caller exit is
# not proof of EOF. No output pump hides that uncertainty.
# After confirmed stdio retirement, return status/fact without adding to the
# callee's stderr: an unread full pipe must not block caller cleanup.
# inference_cancellation_status reports a signal handled by this invocation,
# separately from ordinary client exit129/130/143. Reset it before work and
# publish the final outcome only after restoring the caller's signal traps.
# inference_diagnostic returns controlled wrapper text, not cleanup authority.
# Consume it before another call resets it: callers finish permitted quiet
# cleanup, then report through their held owner. A terminal reporter failure
# exits without retry/direct fallback; delivery can fail during bootstrap.
# Setup, receipt and removal failures return controlled diagnostics without
# utility stderr delaying caller cleanup. The stdio client keeps its streams;
# filesystem hangs and unconfirmed retirement remain separate limits.
# These entries select explicit cancellation contracts and delegate without
# interception to one required-policy implementation and its single result/facts.
inference_native() {
    native_client term-1s "$@"
}

container_native() {
    native_client original-2s "$@"
}

# Controllers own nested cleanup. Forward cancellation and await their existing
# completion receipt; no elapsed grace can establish that cleanup has returned.
# A stuck controller can therefore keep this wait live indefinitely.
controller_native() {
    native_client controller-completion "$@"
}

native_client() {
    local policy=$1
    shift
    local operation=$1 budget=$2 result=$3
    shift 3
    local scratch leader= drain= capture_guard status=0 cancelled=0 drain_status=0 reason
    local saved_traps monitor_enabled=false ready=false retired=true publication_failed=false
    local startup_deadline observed_status timed_program anchor_program owning_parent=$BASHPID
    local join_children=false interrupt_count=0 before_wait removal_status completion_observed=false
    local deadline_signal grace forwarded_signal deadline_grace_started=false
    local release_attempted=false forwarded_interrupt_count=0 before_forward
    inference_cancellation_status=0
    inference_diagnostic=
    inference_result=
    inference_retirement_confirmed=true
    case "$policy" in
        term-1s) deadline_signal=TERM; grace=1 ;;
        original-2s) deadline_signal=USR2; grace=2 ;;
        controller-completion)
            if [[ $budget != untimed || $result != stdio ]]; then
                printf -v inference_diagnostic '%s\n' 'Cannot own controller: cooperative completion requires untimed stdio.'
                return 2
            fi
            deadline_signal=; grace=
            ;;
        *)
            printf -v inference_diagnostic '%s\n' 'Cannot own native client: an explicit supported cancellation policy is required.'
            return 2
            ;;
    esac
    if scratch=$(umask 077; mktemp -d /tmp/nmrnet-inference-native.XXXXXXXX 2>/dev/null); then :; else
        status=$?
        printf -v inference_diagnostic '%sCannot complete %s: private evidence directory creation failed (status %s).\n' "$inference_diagnostic" "$operation" "$status"
        return "$status"
    fi
    if [[ $result != stdio ]]; then
        # Scope stderr on the compound command: redirecting exec itself would
        # replace this shell's public stderr beyond descriptor setup.
        if { mkfifo -- "$scratch/output" && (umask 077; : > "$scratch/command.log") \
            && exec {capture_guard}<> "$scratch/output"; } 2>/dev/null; then :; else
            status=$?
            printf -v inference_diagnostic '%sCannot complete %s: private output setup failed (status %s); retained directory %q.\n' "$inference_diagnostic" "$operation" "$status" "$scratch"
            return "$status"
        fi
    fi
    saved_traps=$(trap -p HUP INT TERM USR1)
    [[ $- != *m* ]] || monitor_enabled=true
    # Flags do not leave either fork-to-$! handoff. HUP must also stay inside
    # this owner until children are retired; the caller's traps are restored.
    trap 'cancelled=129; interrupt_count=$((interrupt_count + 1))' HUP
    trap 'cancelled=130; interrupt_count=$((interrupt_count + 1))' INT
    trap 'cancelled=143; interrupt_count=$((interrupt_count + 1))' TERM
    trap 'publication_failed=true' USR1
    inference_retirement_confirmed=false
    if [[ $result != stdio ]]; then
        (
            exec {capture_guard}>&-
            umask 077
            retained_status=0
            head -c 8388608 > "$scratch/command.log" || retained_status=$?
            cat >/dev/null || {
                discard_status=$?
                if [[ $retained_status == 0 ]]; then retained_status=$discard_status; fi
            }
            exit "$retained_status"
        ) 2>/dev/null < "$scratch/output" >/dev/null &
        drain=$!
    fi
    timed_program='
        receipt=$1 parent=$2 anchor=$3 deadline_signal=$4; shift 4
        umask 077
        expire() {
            # timeout --foreground signals this branch, not the held anchor.
            # Forward to the held group, which contains the actual client;
            # its identity cannot be reused while the outer anchor stays live.
            trap "" TERM
            if { printf "%s\n" expired > "$receipt.deadline.pending" \
                && mv -- "$receipt.deadline.pending" "$receipt.deadline"; } 2>/dev/null; then :; else
                kill -USR1 "$parent" 2>/dev/null
            fi
            kill -TERM -- "-$anchor" 2>/dev/null || :
        }
        trap expire "$deadline_signal"
        (trap - HUP INT TERM; exec "$@") </dev/null &
        child=$!
        if wait "$child"; then status=0; else status=$?; fi
        # A TERM-interrupted wait cannot report a late success as timely.
        if [[ -f "$receipt.deadline" ]]; then
            while :; do sleep 60; done
        fi
        exit "$status"
    '
    anchor_program='
        receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6
        umask 077
        interrupted=false
        trap "interrupted=true" HUP INT TERM
        if { printf "%s\n" "$$" > "$receipt.ready.pending" \
            && mv -- "$receipt.ready.pending" "$receipt.ready"; } 2>/dev/null; then :; else
            status=$?
            # A failed publisher may already have made ready visible. Keep that
            # admitted identity held, without dispatch, until the parent joins.
            # Repeated signals must not interrupt validation or end this anchor.
            trap "" HUP INT TERM
            if [[ -f "$receipt.ready" && ! -L "$receipt.ready" ]] \
                && cmp -s -- "$receipt.ready" <(printf "%s\n" "$$" 2>/dev/null) 2>/dev/null; then
                kill -USR1 "$parent" 2>/dev/null
                while :; do sleep 60; done
            fi
            exit "$status"
        fi
        while [[ ! -e "$receipt.release" && ! -L "$receipt.release" ]]; do sleep 0.01; done
        if [[ ! -f "$receipt.release" || -L "$receipt.release" ]] \
            || ! cmp -s -- "$receipt.release" <(printf "%s\n" "$$" 2>/dev/null) 2>/dev/null; then
            kill -USR1 "$parent" 2>/dev/null
            while :; do sleep 60; done
        fi
        # The supplied operation budget starts at admitted client dispatch.
        # Foreground timeout leaves this outer ownership anchor alive even
        # when its timed branch requires KILL after the one-second TERM grace.
        if [[ "$budget" == untimed ]]; then
            (trap - HUP INT TERM; exec "$@") </dev/null &
        else
            (trap - HUP INT TERM
                exec timeout --foreground --signal="$deadline_signal" --kill-after="$grace" "$budget" \
                    bash -c "$timed_program" bash "$receipt" "$parent" "$$" "$deadline_signal" "$@") &
        fi
        child=$!
        while :; do
            interrupted=false
            if wait "$child"; then status=0; else status=$?; fi
            if ! "$interrupted"; then break; fi
        done
        if { printf "%s\n" "$status" > "$receipt.pending" \
            && mv -- "$receipt.pending" "$receipt"; } 2>/dev/null; then :; else
            kill -USR1 "$parent" 2>/dev/null
        fi
        while :; do sleep 60; done
    '
    # Job control creates the anchor group without asynchronous SIGINT ignore.
    # The checked ready/release and completion-fault protocol is the reviewed
    # tools/container.sh owner pattern, shared by these two output dispositions.
    set -m
    if [[ $result == stdio ]]; then
        bash -c "$anchor_program" bash "$scratch/status" "$owning_parent" "$budget" "$timed_program" "$deadline_signal" "$grace" "$@" &
    else
        (
            exec {capture_guard}>&-
            exec bash -c "$anchor_program" bash "$scratch/status" "$owning_parent" "$budget" "$timed_program" "$deadline_signal" "$grace" "$@"
        ) 2>/dev/null > "$scratch/output" 2>&1 &
    fi
    leader=$!
    set +m
    # This temporary FIFO writer makes failed startup/no native writer yield
    # EOF once closed, so the drainer can always be joined on that failure.
    if [[ $result != stdio ]]; then
        if { exec {capture_guard}>&-; } 2>/dev/null; then :; else
            status=$?
            reason='temporary output writer could not be closed'
        fi
    fi
    startup_deadline=$((SECONDS + 10))
    # Group cancellation can make Bash report its foreground polling sleep's
    # termination after child redirections end. Quiet this control scope only;
    # the supplied native client's streams keep their existing descriptors.
    while [[ ! -f "$scratch/status.ready" ]] && kill -0 "$leader" 2>/dev/null \
        && (( SECONDS < startup_deadline )); do { sleep 0.01; } 2>/dev/null; done
    if [[ -f "$scratch/status.ready" && ! -L "$scratch/status.ready" ]] \
        && cmp -s -- "$scratch/status.ready" <(printf '%s\n' "$leader" 2>/dev/null) 2>/dev/null \
        && kill -0 "$leader" 2>/dev/null; then
        ready=true
    else
        status=1
        reason='native anchor did not publish its exact live identity within 10 seconds'
        retired=false
        # A dead pre-ready child has no native descendants: release was never
        # published. Join it, plus any captured-mode EOF drainer, without
        # guessing group effects.
        if ! kill -0 "$leader" 2>/dev/null; then
            join_children=true
        fi
    fi
    if "$ready" && [[ $status == 0 && $cancelled == 0 ]]; then
        # A failing publication may still have released the controller. Record
        # the attempt before its I/O; only canonical completion then permits KILL.
        if [[ $policy == controller-completion ]]; then release_attempted=true; fi
        if { (umask 077; printf '%s\n' "$leader" > "$scratch/status.release.pending") \
            && mv -- "$scratch/status.release.pending" "$scratch/status.release"; } 2>/dev/null; then
            while [[ ! -e "$scratch/status" && ! -L "$scratch/status" \
                && ! -e "$scratch/status.deadline" && ! -L "$scratch/status.deadline" \
                && $cancelled == 0 ]] && ! "$publication_failed" \
                && kill -0 "$leader" 2>/dev/null; do { sleep 0.01; } 2>/dev/null; done
            if [[ $policy == controller-completion ]]; then
                # Repeated signals change the pending cause and are forwarded
                # without restarting a wait or introducing a cleanup deadline.
                while [[ ! -e "$scratch/status" && ! -L "$scratch/status" \
                    && ! -e "$scratch/status.deadline" && ! -L "$scratch/status.deadline" ]] \
                    && ! "$publication_failed" && kill -0 "$leader" 2>/dev/null; do
                    if [[ $cancelled != 0 && $forwarded_interrupt_count != "$interrupt_count" ]]; then
                        before_forward=$interrupt_count
                        case "$cancelled" in
                            129) forwarded_signal=HUP ;;
                            130) forwarded_signal=INT ;;
                            143) forwarded_signal=TERM ;;
                        esac
                        # A trap between sampling the count and cause gets a
                        # fresh pass; a later trap remains pending after kill.
                        if [[ $before_forward != "$interrupt_count" ]]; then continue; fi
                        if kill "-$forwarded_signal" -- "-$leader" 2>/dev/null; then
                            forwarded_interrupt_count=$before_forward
                        else
                            status=1
                            reason='native controller signal forwarding could not be confirmed'
                            printf -v inference_diagnostic '%sCannot complete %s: %s.\n' "$inference_diagnostic" "$operation" "$reason"
                            retired=false
                            break
                        fi
                    fi
                    { sleep 0.01; } 2>/dev/null
                done
            fi
            if "$publication_failed"; then
                status=1
                reason='native control publication failed; the client status is unobserved'
            elif [[ $policy == controller-completion && ( -e "$scratch/status.deadline" || -L "$scratch/status.deadline" ) ]]; then
                # This policy has no deadline. Even a correctly spelled expiry
                # cannot stand in for the controller's canonical completion.
                status=1
                reason='untimed controller published an unexpected deadline receipt'
                retired=false
            elif [[ -e "$scratch/status.deadline" || -L "$scratch/status.deadline" ]]; then
                # A FIFO/device is not a receipt: reject its type before cmp
                # can block and delay retirement of the held native group.
                if [[ -f "$scratch/status.deadline" && ! -L "$scratch/status.deadline" ]] \
                    && cmp -s -- "$scratch/status.deadline" <(printf '%s\n' expired 2>/dev/null) 2>/dev/null; then
                    status=124
                    reason='native client deadline elapsed'
                    # The timed branch recorded expiry before attempting TERM.
                    # Keep the held anchor through escalation; this receipt
                    # alone does not establish signal delivery to the client.
                    if [[ $policy == original-2s ]]; then
                        # This records entry to an actual expiry-grace phase,
                        # not a second deadline or retirement authority. A known
                        # cancellation must reach original-signal forwarding now.
                        if [[ $cancelled == 0 ]]; then
                            deadline_grace_started=true
                            sleep "$grace" || :
                        fi
                    else
                        sleep "$grace"
                    fi
                else
                    status=1
                    reason='native deadline receipt is not byte-exact'
                fi
            elif [[ $cancelled == 0 || $policy == controller-completion ]] && "$retired"; then
                if [[ -f "$scratch/status" && ! -L "$scratch/status" ]] \
                    && { observed_status=$(head -c 5 -- "$scratch/status" 2>/dev/null); } 2>/dev/null; then
                    if [[ "$observed_status" =~ ^(0|[1-9][0-9]{0,2})$ && $observed_status -le 255 ]] \
                        && cmp -s -- "$scratch/status" <(printf '%s\n' "$observed_status" 2>/dev/null) 2>/dev/null; then
                        status=$observed_status
                        completion_observed=true
                        reason="native client exited with status $status"
                    else
                        status=2
                        reason='native completion receipt must contain one byte-exact status from 0 through 255'
                    fi
                else
                    status=1
                    reason='native completion receipt could not be read'
                fi
            fi
        else
            status=$?
            reason='native parent release could not be published'
        fi
    fi
    if [[ $policy == controller-completion ]] && "$release_attempted" && ! "$completion_observed"; then
        # Missing, malformed or uncommitted completion cannot authorize killing
        # a controller that may still be removing its engine-owned resources.
        retired=false
        if [[ $status == 0 ]]; then
            status=1
            reason='native controller completion could not be confirmed'
        fi
    fi
    if "$ready" && "$retired"; then
        # The existing entry offers untimed TERM/1s; container's entry forwards
        # the actual signal with 2s for either budget. Only its numeric expiry
        # uses USR2, so group TERM cannot manufacture the expired receipt.
        # Repeated signals may shorten grace, but never renew it. The held
        # anchor remains authoritative through escalation and exact joining.
        forwarded_signal=TERM
        if [[ $policy == original-2s ]]; then
            case "$cancelled" in
                129) forwarded_signal=HUP ;;
                130) forwarded_signal=INT ;;
                143) forwarded_signal=TERM ;;
            esac
        fi
        if [[ $policy != controller-completion && $cancelled != 0 && ( $budget == untimed || $policy == original-2s ) ]] \
            && kill -0 "$leader" 2>/dev/null && kill "-$forwarded_signal" -- "-$leader" 2>/dev/null; then
            if ! "$deadline_grace_started"; then sleep "$grace" || :; fi
        fi
        if kill -0 "$leader" 2>/dev/null && kill -KILL -- "-$leader" 2>/dev/null; then
            join_children=true
        else
            retired=false
            if [[ $status == 0 ]]; then status=1; fi
            reason='native anchor retirement could not be confirmed'
        fi
    fi
    if "$join_children"; then
        # A trapped signal interrupts wait without joining its child. Retry the
        # exact child until one wait completes without a new cancellation trap.
        while :; do
            before_wait=$interrupt_count
            wait "$leader" 2>/dev/null || :
            if [[ $before_wait == "$interrupt_count" ]]; then break; fi
        done
        if [[ $result != stdio ]]; then
            while :; do
                before_wait=$interrupt_count
                if wait "$drain" 2>/dev/null; then drain_status=0; else drain_status=$?; fi
                if [[ $before_wait == "$interrupt_count" ]]; then break; fi
            done
        fi
        # Publish only after this mode's children join: the anchor in stdio,
        # plus its drainer in captured modes. Cleanup or trap restoration can
        # fail or be interrupted without making those retired processes live.
        if "$retired"; then inference_retirement_confirmed=true; fi
    fi
    if [[ $status == 0 && $drain_status != 0 ]]; then
        status=$drain_status
        reason='native output could not be retained or drained'
    fi
    if [[ $cancelled != 0 ]]; then
        status=$cancelled
        reason="native client was interrupted (status $status)"
    fi
    if [[ $status == 0 && $result == image-id ]]; then
        if [[ $(wc -c 2>/dev/null < "$scratch/command.log") != 72 ]] \
            || ! LC_ALL=C grep -a -x -E 'sha256:[0-9a-f]{64}' "$scratch/command.log" >/dev/null 2>&1 \
            || ! IFS= read -r inference_result 2>/dev/null < "$scratch/command.log" \
            || ! cmp -s -- "$scratch/command.log" <(printf '%s\n' "$inference_result" 2>/dev/null) 2>/dev/null; then
            status=2
            reason='native identity output did not contain exactly one full immutable image ID'
            inference_result=
        fi
    fi
    if [[ $result == stdio ]] && "$retired"; then
        # Native output owns the public streams. Control cleanup is quiet too;
        # its failure changes only success, never native/cancellation status.
        # Joining does not erase a control failure's cause. Canonical native
        # completion and actual cancellation keep their existing stream contract.
        if [[ $status != 0 && $cancelled == 0 ]] && ! "$completion_observed"; then
            printf -v inference_diagnostic '%sCannot complete %s: %s.\n' "$inference_diagnostic" "$operation" "$reason"
        fi
        if rm -rf -- "$scratch" 2>/dev/null; then :; else
            removal_status=$?
            if [[ $status == 0 ]]; then status=$removal_status; fi
            printf -v inference_diagnostic '%sCannot confirm native client evidence directory removal; attempted path %q (status %s).\n' \
                "$inference_diagnostic" "$scratch" "$removal_status"
        fi
    elif [[ $status != 0 ]]; then
        if "$retired"; then
            if rm -f -- "$scratch/output" "$scratch"/status* 2>/dev/null; then :; else
                printf -v inference_diagnostic '%sCannot remove native client control files; retained directory %q.\n' "$inference_diagnostic" "$scratch"
            fi
        else
            printf -v inference_diagnostic '%sCannot confirm native client retirement; retained directory %q.\n' "$inference_diagnostic" "$scratch"
        fi
        if [[ $result == stdio ]]; then
            printf -v inference_diagnostic '%sCannot complete %s: %s.\n' "$inference_diagnostic" "$operation" "$reason"
        else
            printf -v inference_diagnostic '%sCannot complete %s: %s; private capped output retained at %q.\n' \
                "$inference_diagnostic" "$operation" "$reason" "$scratch/command.log"
        fi
    elif rm -rf -- "$scratch" 2>/dev/null; then :; else
        status=$?
        printf -v inference_diagnostic '%sCannot confirm native client evidence directory removal; attempted path %q (status %s).\n' "$inference_diagnostic" "$scratch" "$status"
    fi
    if [[ $cancelled != 0 && $status != "$cancelled" ]]; then
        status=$cancelled
        if [[ $result != stdio ]]; then
            printf -v inference_diagnostic '%sCannot complete %s: native client was interrupted during cleanup (status %s).\n' "$inference_diagnostic" "$operation" "$status"
        fi
    fi
    trap - HUP INT TERM USR1
    if [[ -n "$saved_traps" ]]; then eval "$saved_traps"; fi
    if "$monitor_enabled"; then set -m; fi
    inference_cancellation_status=$cancelled
    if [[ $inference_cancellation_status != 0 ]]; then status=$inference_cancellation_status; fi
    return "$status"
}
