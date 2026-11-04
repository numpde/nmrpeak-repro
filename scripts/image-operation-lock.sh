#!/usr/bin/env bash

# Coordinate this checkout's image publication, deployment and retirement.
hold_image_operation_lock() {
    local root=$1
    exec {image_operation_lock_fd}< "$root"
    flock --shared --wait 60 "$image_operation_lock_fd" || {
        printf '%s\n' 'Cannot begin image work: the checkout image lock is busy or unavailable.' >&2
        return 2
    }
}

image_capture_directory() {
    local root=$1 parent="$1/.work" captures="$1/.work/image-builds"
    [[ -e "$parent" ]] || mkdir -m 700 -- "$parent" 2>/dev/null || [[ -d "$parent" ]]
    [[ -d "$parent" && ! -L "$parent" && -O "$parent" ]] || {
        printf '%s\n' 'Cannot prepare image capture: the workspace directory is not owned and local.' >&2
        return 2
    }
    [[ -e "$captures" ]] || mkdir -m 700 -- "$captures" 2>/dev/null || [[ -d "$captures" ]]
    [[ -d "$captures" && ! -L "$captures" && -O "$captures" && "$(stat -c %a -- "$captures")" == 700 ]] || {
        printf '%s\n' 'Cannot prepare image capture: the capture directory is not private and owned.' >&2
        return 2
    }
    mktemp -d "$captures/build.XXXXXXXX"
}

retire_image_worker() {
    local receipt=$1 expected=$2 cid state running code label extra
    [[ -s "$receipt" ]] || return 1
    cid="$(<"$receipt")"
    [[ "$cid" =~ ^[0-9a-f]{64}$ ]] || return 1
    state="$(command docker --host "$NMRPEAK_DOCKER_HOST" inspect "$cid" --format '{{.State.Running}} {{.State.ExitCode}} {{index .Config.Labels "io.numpde.nmrpeak.wheel-report"}}')" || return 1
    read -r running code label extra <<<"$state"
    [[ "$running" == false && "$code" =~ ^[0-9]+$ && "$label" == "$expected" && -z "$extra" ]] || return 1
    command docker --host "$NMRPEAK_DOCKER_HOST" rm "$cid" >/dev/null || return 1
    image_worker_exit_code=$code
}

trim_streamed_image_capture() {
    find "$1" -mindepth 1 -maxdepth 1 \
        ! -name base.id ! -name image.id ! -name proxy.port ! -name proxy.log ! -name resolv.conf \
        -exec rm -rf -- {} +
}
