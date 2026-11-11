#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 1 ]] || { echo 'usage: test-image-key.sh <repository-root>' >&2; exit 2; }
readonly root="$1"
sha256sum \
    "$root/containers/test/base.env" \
    "$root/bootstrap/acquisition/acquisition_tool/installer/requirements/installer-release.json" \
    "$root/bootstrap/acquisition/acquisition_tool/installer/tools/container_guard.py" \
    "$root/bootstrap/acquisition/acquisition_tool/installer/tools/installer.py" \
    "$root/bootstrap/acquisition/acquisition_tool/installer/wheels/pip-26.2.2-py3-none-any.whl" \
    "$root/containers/test/Dockerfile.base" \
    "$root/containers/test/os-packages.lock" \
    "$root/requirements.lock" \
    "$root/dependencies/provider-core-api.lock" \
    "$root/dependencies/provider-core-source.txt" \
    "$root/repository_checks/wheelhouse_tool_asset.py" \
    "$root/repository_checks/image_wheel_report.py" \
    "$root/scripts/test-image-key.sh" \
    | awk '{print $1}' | sha256sum | awk '{print $1}'
