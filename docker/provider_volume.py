#!/usr/bin/env python3
"""Initialize or verify one fixed provider-owned Docker volume."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat


VOLUME_PATH = Path("/volume")
IDENTITY_LOCK_PATH = VOLUME_PATH / "provider.lock"
JOURNAL_PATH = VOLUME_PATH / "journal"
_PROVIDER_REF = re.compile(r"provider:[A-Za-z0-9_.-]{1,119}")


class ProviderVolumeRejected(RuntimeError):
    """A provider-owned volume cannot be initialized or admitted."""


def admit_identity_lock(provider_ref: str) -> None:
    """Create once or verify the root-owned provider identity lock file."""

    if type(provider_ref) is not str or _PROVIDER_REF.fullmatch(provider_ref) is None:
        raise ProviderVolumeRejected("provider identity is invalid")
    expected = provider_ref.encode("ascii") + b"\n"
    names = _volume_names()
    if not names:
        descriptor = os.open(
            IDENTITY_LOCK_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
        )
        try:
            if os.write(descriptor, expected) != len(expected):
                raise OSError("provider identity lock write was incomplete")
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o444)
        finally:
            os.close(descriptor)
        _fsync_volume()
    if _volume_names() != {IDENTITY_LOCK_PATH.name}:
        raise ProviderVolumeRejected("provider identity lock inventory is invalid")
    descriptor = _open_regular(IDENTITY_LOCK_PATH)
    try:
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or os.read(descriptor, 130) != expected
        ):
            raise ProviderVolumeRejected("provider identity lock has drifted")
    finally:
        os.close(descriptor)


def admit_journal() -> None:
    """Create once or verify the provider-owned journal root directory."""

    names = _volume_names()
    if not names:
        JOURNAL_PATH.mkdir(mode=0o700)
        os.chown(JOURNAL_PATH, 65532, 65532, follow_symlinks=False)
        _fsync_volume()
    if _volume_names() != {JOURNAL_PATH.name}:
        raise ProviderVolumeRejected("provider journal volume inventory is invalid")
    try:
        metadata = JOURNAL_PATH.lstat()
    except OSError as error:
        raise ProviderVolumeRejected("provider journal root is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 65532
        or metadata.st_gid != 65532
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ProviderVolumeRejected("provider journal root posture has drifted")


def _volume_names() -> set[str]:
    try:
        return {entry.name for entry in VOLUME_PATH.iterdir()}
    except OSError as error:
        raise ProviderVolumeRejected("provider volume is unreadable") from error


def _open_regular(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ProviderVolumeRejected("provider volume file is unavailable") from error
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ProviderVolumeRejected("provider volume file is not regular")
    return descriptor


def _fsync_volume() -> None:
    descriptor = os.open(VOLUME_PATH, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("identity-lock", "journal"))
    parser.add_argument("provider_ref", nargs="?")
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "identity-lock":
            if arguments.provider_ref is None:
                parser.error("identity-lock requires one provider reference")
            admit_identity_lock(arguments.provider_ref)
        else:
            if arguments.provider_ref is not None:
                parser.error("journal does not accept a provider reference")
            admit_journal()
    except (OSError, ProviderVolumeRejected) as error:
        parser.exit(2, f"Provider volume rejected: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
