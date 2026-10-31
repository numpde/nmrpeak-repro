#!/usr/bin/env bash
# Select one available interface without assuming the host's default route.

select_network_interface() {
    local name path answer index tty_fd state
    local -a choices=(system)
    if [[ ${NMRPEAK_NETWORK_INTERFACE_INPUT_ORIGIN:-} == 'command line' ]]; then
        NETWORK_INTERFACE_SELECTED=${NMRPEAK_NETWORK_INTERFACE_INPUT-}
    else
        if [[ -n ${NMRPEAK_NETWORK_INTERFACE_INPUT_ORIGIN:-} && ${NMRPEAK_NETWORK_INTERFACE_INPUT_ORIGIN} != undefined ]]; then
            printf '%s\n' 'NETWORK_INTERFACE must be supplied on the make command line or chosen interactively.' >&2
            return 2
        fi
        for path in /sys/class/net/*; do
            name=${path##*/}
            [[ $name =~ ^[A-Za-z0-9_.:-]{1,15}$ && $name != . && $name != .. && -d $path ]] && choices+=("$name")
        done
        if ! { exec {tty_fd}<>/dev/tty; } 2>/dev/null; then
            printf '%s\n' 'Select a network interface with NETWORK_INTERFACE=<interface> on the make command line; no interactive terminal is available.' >&2
            for name in "${choices[@]}"; do printf '  NETWORK_INTERFACE=%s\n' "$name" >&2; done
            return 2
        fi
        printf 'Select NETWORK_INTERFACE (no default):\n' >&"$tty_fd"
        printf 'system uses the host default route; other entries name local interfaces.\n' >&"$tty_fd"
        for index in "${!choices[@]}"; do
            name=${choices[index]}
            if [[ $name == system ]]; then
                printf '  %d) system (default route)\n' "$((index + 1))" >&"$tty_fd"
            else
                state=unavailable
                IFS= read -r state < "/sys/class/net/$name/operstate" 2>/dev/null || state=unavailable
                printf '  %d) %s (state: %s)\n' "$((index + 1))" "$name" "$state" >&"$tty_fd"
            fi
        done
        printf 'Choice [1-%d]: ' "${#choices[@]}" >&"$tty_fd"
        if ! IFS= read -r answer <&"$tty_fd"; then
            exec {tty_fd}>&-
            printf '%s\n' 'NETWORK_INTERFACE selection ended before a choice was read.' >&2
            return 2
        fi
        exec {tty_fd}>&-
        NETWORK_INTERFACE_SELECTED=
        for index in "${!choices[@]}"; do
            if [[ $answer == "$((index + 1))" ]]; then NETWORK_INTERFACE_SELECTED=${choices[index]}; break; fi
        done
    fi
    if [[ $NETWORK_INTERFACE_SELECTED != system ]] && [[ ! $NETWORK_INTERFACE_SELECTED =~ ^[A-Za-z0-9_.:-]{1,15}$ || $NETWORK_INTERFACE_SELECTED == . || $NETWORK_INTERFACE_SELECTED == .. || ! -d /sys/class/net/$NETWORK_INTERFACE_SELECTED ]]; then
        printf 'NETWORK_INTERFACE=%q is not an available network interface.\n' "$NETWORK_INTERFACE_SELECTED" >&2
        return 2
    fi
    export NETWORK_INTERFACE_SELECTED
}

require_local_docker() {
    [[ "${NMRPEAK_DOCKER_HOST:-}" =~ ^unix:///[^[:space:]]+$ ]] || {
        printf '%s\n' 'Run this Docker operation through make with a selected local Unix-socket context.' >&2
        return 2
    }
}
