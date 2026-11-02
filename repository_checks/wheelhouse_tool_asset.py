"""Acquire the pinned shared wheelhouse tool without replacing pool files."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import time


TOOL_NAME = "wheelhouse-tool-0.5.8.pyz"
TOOL_SHA256 = "bb37f04420772b553abb694ef167414a2ec03fdfaf8e6ab802aaf37894d0bb37"
TOOL_URL = "https://github.com/numpde/nmr-provider-tools/releases/download/v0.5.8/" + TOOL_NAME
MAX_BYTES = 16 * 1024 * 1024


def _lock(directory: int) -> None:
    deadline = time.monotonic() + 300
    while True:
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("The shared wheelhouse remained busy for 300 seconds.")
            time.sleep(0.1)


def _present(directory: int, name: str, expected: str) -> bool:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Cannot use {name}: expected a regular file.")
        if metadata.st_size > MAX_BYTES:
            raise ValueError(f"Cannot use {name}: existing file exceeds 16 MiB and was preserved.")
        if hashlib.file_digest(source, "sha256").hexdigest() != expected:
            raise ValueError(f"Cannot use {name}: existing bytes differ from the pinned release; they were preserved.")
    return True


def _download(output: int, directory: int, interface: str) -> None:
    command = ["curl", "--disable", "--fail", "--silent", "--show-error", "--location",
               "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "30",
               "--max-time", "300", "--max-filesize", str(MAX_BYTES)]
    if interface != "system":
        command.extend(("--interface", interface))
    command.extend(("--output", "-", TOOL_URL))

    try:
        subprocess.run(command, stdout=output, stderr=subprocess.DEVNULL, check=True,
                       timeout=330, pass_fds=(directory,))
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("The shared wheelhouse tool download did not complete.") from error


def ensure_tool(pool: Path, interface: str | None, *, name: str = TOOL_NAME,
                expected: str = TOOL_SHA256, download=_download) -> bool:
    """Return false only when the tool is absent and no network route was supplied."""
    if interface is not None and interface != "system" and (
        interface in (".", "..") or re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface) is None
    ):
        raise ValueError("The selected network interface is invalid.")
    directory = os.open(pool, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _lock(directory)
        if _present(directory, name, expected):
            return True
        if interface is None:
            return False
        temporary = f".wheelhouse-tool-{secrets.token_hex(16)}.part"
        descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "w+b") as output:
                download(output.fileno(), directory, interface)
                if os.fstat(output.fileno()).st_size > MAX_BYTES:
                    raise ValueError("The shared wheelhouse tool exceeds 16 MiB.")
                output.seek(0)
                if hashlib.file_digest(output, "sha256").hexdigest() != expected:
                    raise ValueError("The downloaded wheelhouse tool differs from the pinned release.")
                os.fchmod(output.fileno(), 0o644)
                os.fsync(output.fileno())
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if not _present(directory, name, expected):
                    raise FileNotFoundError("The competing wheelhouse tool disappeared.")
            os.fsync(directory)
        finally:
            os.unlink(temporary, dir_fd=directory)
        return True
    finally:
        os.close(directory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "acquire"))
    parser.add_argument("pool", type=Path)
    parser.add_argument("--interface")
    args = parser.parse_args()
    if (args.action == "acquire") != (args.interface is not None):
        parser.error("acquire requires an interface; check does not accept one")

    signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)

    def interrupted(_signum, _frame):
        for number in signals:
            signal.signal(number, signal.SIG_IGN)
        raise InterruptedError("The shared wheelhouse tool operation was interrupted.")

    previous = {number: signal.signal(number, interrupted) for number in signals}
    try:
        return 0 if ensure_tool(args.pool, args.interface) else 10
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
