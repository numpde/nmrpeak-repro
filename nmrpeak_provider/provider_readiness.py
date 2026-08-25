"""Project one provider boot's local readiness into its private runtime tmpfs."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat


STATUS_PATH = Path("/run/nmrpeak-provider/ready")
_CONTENT = b"ready\n"


class ProviderReadiness:
    """Own the exact readiness file published by one process boot."""

    def __init__(self, parent_fd: int, leaf: str) -> None:
        self._parent_fd = parent_fd
        self._leaf = leaf
        self._published_fd = -1

    @classmethod
    def begin(cls, path: Path = STATUS_PATH) -> ProviderReadiness:
        """Clear one admissible stale marker before startup admission begins."""

        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        readiness = cls(parent_fd, path.name)
        try:
            readiness._clear_stale()
        except BaseException:
            os.close(parent_fd)
            raise
        return readiness

    def publish(self) -> None:
        """Atomically expose readiness after this boot's startup proof succeeds."""

        if self._published_fd >= 0:
            raise RuntimeError("Provider readiness is already published")
        temporary = f".{self._leaf}.{secrets.token_hex(16)}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
            dir_fd=self._parent_fd,
        )
        try:
            if os.write(descriptor, _CONTENT) != len(_CONTENT):
                raise OSError("Provider readiness write was incomplete")
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o444)
            os.rename(
                temporary,
                self._leaf,
                src_dir_fd=self._parent_fd,
                dst_dir_fd=self._parent_fd,
            )
            self._published_fd = descriptor
            descriptor = -1
            os.fsync(self._parent_fd)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._parent_fd)
            except FileNotFoundError:
                pass
            raise

    def close(self) -> None:
        """Remove only this boot's published marker and close directory authority."""

        try:
            if self._published_fd >= 0:
                owned = os.fstat(self._published_fd)
                metadata = os.stat(
                    self._leaf,
                    dir_fd=self._parent_fd,
                    follow_symlinks=False,
                )
                if (metadata.st_dev, metadata.st_ino) != (
                    owned.st_dev,
                    owned.st_ino,
                ):
                    raise RuntimeError("Provider readiness changed before cleanup")
                os.unlink(self._leaf, dir_fd=self._parent_fd)
                os.fsync(self._parent_fd)
        finally:
            if self._published_fd >= 0:
                os.close(self._published_fd)
                self._published_fd = -1
            os.close(self._parent_fd)
            self._parent_fd = -1

    def _clear_stale(self) -> None:
        try:
            metadata = os.stat(
                self._leaf,
                dir_fd=self._parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o444
        ):
            raise RuntimeError("Stale provider readiness is not an owned mode-0444 file")
        descriptor = os.open(
            self._leaf,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=self._parent_fd,
        )
        try:
            if os.read(descriptor, len(_CONTENT) + 1) != _CONTENT:
                raise RuntimeError("Stale provider readiness has invalid content")
        finally:
            os.close(descriptor)
        os.unlink(self._leaf, dir_fd=self._parent_fd)
        os.fsync(self._parent_fd)


def is_provider_ready(path: Path = STATUS_PATH) -> bool:
    """Accept only the exact regular readiness file exposed to health checks."""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        metadata = os.fstat(descriptor)
        return (
            stat.S_ISREG(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == 0o444
            and os.read(descriptor, len(_CONTENT) + 1) == _CONTENT
        )
    finally:
        os.close(descriptor)


def main() -> int:
    return 0 if is_provider_ready() else 1


if __name__ == "__main__":
    raise SystemExit(main())
