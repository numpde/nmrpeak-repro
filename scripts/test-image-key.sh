#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 1 ]] || { echo 'usage: test-image-key.sh <repository-root>' >&2; exit 2; }
readonly root="$1"
sha256sum \
    "$root/containers/test/base.env" \
    "$root/containers/test/Dockerfile.base" \
    "$root/containers/test/os-packages.lock" \
    "$root/requirements.lock" \
    "$root/scripts/test-image-key.sh" \
    | awk '{print $1}' | sha256sum | awk '{print $1}'
