"""Hold the cooperating host's provider-identity singleton for process life."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import stat


_PROVIDER_REF = re.compile(r"provider:[A-Za-z0-9_.-]{1,119}")
_MAX_LOCK_BYTES = 129


class ProviderIdentityLockBusy(RuntimeError):
    """Another cooperating deployment already holds this provider identity."""


class ProviderIdentityLock:
    """The held descriptor proving one cooperating provider process is active."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    @classmethod
    def acquire(cls, path: Path, provider_ref: str) -> ProviderIdentityLock:
        """Open and exclusively lock the existing root-owned identity file."""

        if type(provider_ref) is not str or _PROVIDER_REF.fullmatch(provider_ref) is None:
            raise ValueError("Provider identity lock requires a valid provider reference")
        try:
            descriptor = os.open(
                Path(path),
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError:
            raise ValueError("Provider identity lock file is unavailable") from None
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != 0
                or stat.S_IMODE(status.st_mode) != 0o444
                or status.st_size > _MAX_LOCK_BYTES
            ):
                raise ValueError(
                    "Provider identity lock must be a root-owned mode-0444 regular file"
                )
            content = os.read(descriptor, _MAX_LOCK_BYTES + 1)
            if content != provider_ref.encode("ascii") + b"\n":
                raise ValueError("Provider identity lock names another provider")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProviderIdentityLockBusy(
                    "Another deployment already holds this provider identity"
                ) from None
            return cls(descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        """Release this process's cooperating-provider exclusion exactly once."""

        descriptor = self._descriptor
        if descriptor < 0:
            return
        self._descriptor = -1
        os.close(descriptor)

    def __enter__(self) -> ProviderIdentityLock:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
