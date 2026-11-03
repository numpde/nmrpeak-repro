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
