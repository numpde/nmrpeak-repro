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
    """Keep exclusion until confirmed settlement or OS process teardown.

    This integer descriptor deliberately has no object-finalizer close: failed
    runtime cleanup can leave scientific threads alive after Python unwinding.
    """

    def __init__(self, descriptor: int, provider_ref: str) -> None:
        self._descriptor = descriptor
        self.provider_ref = provider_ref
        self._close_failure: BaseException | None = None

    @classmethod
    def acquire(cls, path: Path, provider_ref: str) -> ProviderIdentityLock:
        """Open and exclusively lock the existing root-owned identity file."""

        if type(provider_ref) is not str or _PROVIDER_REF.fullmatch(provider_ref) is None:
            raise ValueError("Provider identity lock requires a valid provider reference")
        return cls._acquire(path, provider_ref)

    @classmethod
    def acquire_volume(cls, path: Path) -> ProviderIdentityLock:
        """Acquire the volume identity before reading provider-owned inputs."""
        return cls._acquire(path, None)

    @classmethod
    def _acquire(cls, path: Path, expected_ref: str | None) -> ProviderIdentityLock:
        try:
            descriptor = os.open(
                Path(path),
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as error:
            raise ValueError("Provider identity lock file is unavailable") from error
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
            try:
                provider_ref = content.decode("ascii").removesuffix("\n")
            except UnicodeDecodeError as error:
                raise ValueError("Provider identity lock contains an invalid identity") from error
            if content != provider_ref.encode("ascii") + b"\n" or _PROVIDER_REF.fullmatch(provider_ref) is None:
                raise ValueError("Provider identity lock contains an invalid identity")
            if expected_ref is not None and provider_ref != expected_ref:
                raise ValueError("Provider identity lock names another provider")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ProviderIdentityLockBusy(
                    "Another deployment already holds this provider identity"
                ) from error
            return cls(descriptor, provider_ref)
        except BaseException as primary:
            try:
                os.close(descriptor)
            except BaseException as cleanup:
                if cleanup is not primary:
                    raise BaseExceptionGroup(
                        "Provider identity admission and descriptor cleanup failed", [primary, cleanup],
                    ) from None
                raise
            raise

    def close(self) -> None:
        """Attempt release once and retain any uncertainty about its outcome."""

        if self._close_failure is not None:
            raise self._close_failure
        descriptor = self._descriptor
        if descriptor < 0:
            return
        self._descriptor = -1
        try:
            os.close(descriptor)
        except BaseException as cause:
            self._close_failure = cause
            raise

    def __enter__(self) -> ProviderIdentityLock:
        return self

    def __exit__(self, exception_type, primary, traceback) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if primary is not None and cleanup is not primary:
                raise BaseExceptionGroup(
                    "Provider failure and independent identity-lock cleanup failure", [primary, cleanup],
                ) from None
            raise
