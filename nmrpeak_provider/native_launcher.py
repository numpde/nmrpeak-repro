"""Provision private native resources and enter the SDK's sole child owner."""

import argparse
import os
from pathlib import Path

from provider_core.runner import LOCK_NAME, main as run_native_owner
from nmrpeak_provider import native_policy
from nmrpeak_provider.owner_session_endpoint import open_owner_session_directory
from nmrpeak_provider.runner_protocol import RUNNER_SOCKET_PATH


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role")
    parser.add_argument("science_module")
    parser.add_argument("science_arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    scratch = Path("/tmp/nmrpeak-native-scratch")
    status = Path("/tmp/nmrpeak-native-status")
    for directory in (scratch, status):
        directory.mkdir(mode=0o700, exist_ok=True)
    parent, _ = open_owner_session_directory(RUNNER_SOCKET_PATH)
    try:
        try:
            descriptor = os.open(
                LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=parent,
            )
        except FileExistsError:
            # Admission of the existing stable inode belongs to the SDK.
            pass
        else:
            os.close(descriptor)
        return run_native_owner([
            "--role", options.role, "--socket", RUNNER_SOCKET_PATH,
            "--scratch", str(scratch), "--status-directory", str(status),
            "--owner-session-lifetime", "--startup-seconds", str(native_policy.STARTUP_SECONDS),
            "--terminate-seconds", str(native_policy.TERMINATE_SECONDS),
            "--kill-seconds", str(native_policy.KILL_SECONDS),
            "--cleanup-seconds", str(native_policy.CLEANUP_SECONDS), "--",
            "/usr/local/bin/python3.12", "-m", options.science_module, *options.science_arguments,
        ])
    finally:
        os.close(parent)


if __name__ == "__main__":
    raise SystemExit(main())
