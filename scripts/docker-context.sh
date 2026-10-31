#!/usr/bin/env bash
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 2; }
valid_name() { [[ "$1" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; }

inspect_local_endpoint() {
    local context="$1" endpoint
    valid_name "$context" || return 1
    endpoint="$(env -u DOCKER_CONTEXT -u DOCKER_HOST docker --context "$context" context inspect "$context" --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" || return 1
    [[ "$endpoint" =~ ^unix:///[^[:space:]]+$ ]] || return 1
    printf '%s' "$endpoint"
}

local_contexts() {
    local context endpoint
    while IFS= read -r context; do
        [[ -n "$context" ]] || continue
        if endpoint="$(inspect_local_endpoint "$context")"; then
            printf '%s\t%s\n' "$context" "$endpoint"
        fi
    done < <(env -u DOCKER_CONTEXT -u DOCKER_HOST docker context ls --format '{{.Name}}' 2>/dev/null)
}

select_docker_context() {
    local origin="${1:-undefined}"
    local requested="${2:-}" answer tty_fd index selected_endpoint
    local -a contexts=() names=()

    if [[ "$origin" == 'command line' ]]; then
        valid_name "$requested" || fail 'DOCKER_CONTEXT must be a valid Docker context name supplied on the make command line.'
        selected_endpoint="$(inspect_local_endpoint "$requested")" || fail "DOCKER_CONTEXT=$requested must name an existing context that uses a local Unix socket."
        printf '%s' "$selected_endpoint"
        return
    fi
    if [[ "$origin" != undefined ]]; then
        fail 'DOCKER_CONTEXT from the environment or Makefile is implicit; choose it on the make command line or select a context interactively.'
    fi

    mapfile -t contexts < <(local_contexts)
    for answer in "${contexts[@]}"; do names+=("${answer%%$'\t'*}"); done
    ((${#names[@]})) || fail 'No local Unix-socket Docker contexts are available.'
    if ! { exec {tty_fd}<>/dev/tty; } 2>/dev/null; then
        fail 'Set DOCKER_CONTEXT=<name> on the make command line; no interactive terminal is available.'
    fi
    printf '%s\n' 'Select a local Unix-socket Docker context (no default):' >&"$tty_fd"
    for index in "${!contexts[@]}"; do
        printf '  %d) %s\n' "$((index + 1))" "${contexts[index]%%$'\t'*}" >&"$tty_fd"
    done
    printf 'Choice [1-%d]: ' "${#contexts[@]}" >&"$tty_fd"
    if ! IFS= read -r answer <&"$tty_fd"; then
        exec {tty_fd}>&-
        fail 'Docker context selection ended before a choice was read.'
    fi
    exec {tty_fd}>&-
    [[ "$answer" =~ ^[0-9]+$ ]] && ((answer >= 1 && answer <= ${#contexts[@]})) || fail 'Choose one listed Docker context number.'
    selected_endpoint="${contexts[answer - 1]#*$'\t'}"
    printf '%s' "$selected_endpoint"
}

[[ $# -eq 2 ]] || fail 'Docker context selector requires Make variable origin and value.'
select_docker_context "$1" "$2"
