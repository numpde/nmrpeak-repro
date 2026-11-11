"""Acquire the pinned shared wheelhouse tool without replacing pool files."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import time


TOOL_NAME = "wheelhouse-tool-0.6.15.pyz"
TOOL_SHA256 = "8c8ad0ca2c1ee6147e6da525bc302254935b606726bfe4b0ec1ee9021371d58c"
TOOL_URL = "https://github.com/numpde/nmr-provider-tools/releases/download/v0.6.15/" + TOOL_NAME
MAX_BYTES = 16 * 1024 * 1024
MAX_ATTEMPTS = 3


class TransientToolDownload(Exception):
    def __init__(self, detail: str, retry_after: float = 0) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


class ToolInterrupted(Exception):
    pass


def _retry_after(value: str) -> float:
    if not value:
        return 0
    try:
        if value.isascii() and value.isdecimal():
            delay = int(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                raise ValueError
            delay = max(0, math.ceil((date - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Cannot acquire the wheelhouse tool: invalid Retry-After response.") from None
    if delay > 30:
        raise ValueError("Cannot acquire the wheelhouse tool: Retry-After exceeds 30 seconds.")
    return delay


def _lock(directory: int, mode: int) -> None:
    deadline = time.monotonic() + 300
    while True:
        try:
            fcntl.flock(directory, mode | fcntl.LOCK_NB)
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
    command = ["curl", "--disable", "--fail", "--silent", "--location",
               "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "30",
               "--max-time", "300", "--max-filesize", str(MAX_BYTES),
               "--write-out", "%{stderr}%{http_code}|%{http_connect}|%header{retry-after}"]
    if interface != "system":
        command.extend(("--interface", interface))
    command.extend(("--output", "-", TOOL_URL))

    try:
        result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, check=False,
                                timeout=330, pass_fds=(directory,))
    except subprocess.TimeoutExpired:
        raise TransientToolDownload("the transfer timed out") from None
    except OSError:
        raise ValueError("Cannot start the shared wheelhouse tool downloader.") from None
    if result.returncode == 0:
        return
    metadata = result.stderr
    if len(metadata) > 128:
        raise ValueError("Cannot acquire the wheelhouse tool: invalid response metadata.")
    match = re.fullmatch(rb"([0-9]{3})\|([0-9]{3})\|([^\r\n]*)", metadata)
    if match is None:
        raise ValueError("Cannot acquire the wheelhouse tool: response status is unavailable.")
    status, tunnel = int(match[1]), int(match[2])
    if status == 0 and tunnel in (429, 503):
        raise ValueError(f"Cannot acquire the wheelhouse tool: proxy tunnel HTTP {tunnel}; Retry-After is unavailable.")
    if status == 0 and tunnel >= 300 and tunnel not in (408, 500, 502, 504):
        raise ValueError(f"Cannot acquire the wheelhouse tool: proxy tunnel HTTP {tunnel}.")
    status = status or tunnel
    if (result.returncode == 22 or (result.returncode == 56 and status == tunnel)) and status in (408, 429, 500, 502, 503, 504):
        try:
            retry_after = _retry_after(match[3].decode("ascii")) if status in (429, 503) else 0
        except UnicodeDecodeError:
            raise ValueError("Cannot acquire the wheelhouse tool: invalid Retry-After response.") from None
        raise TransientToolDownload(f"HTTP {status}", retry_after)
    if result.returncode in (7, 18, 28, 52, 55, 56):
        raise TransientToolDownload("the connection or transfer failed")
    if result.returncode in (5, 6):
        raise ValueError("Cannot acquire the wheelhouse tool: name resolution failed; curl cannot classify it as temporary.")
    if result.returncode in (51, 60, 77):
        raise ValueError("Cannot acquire the wheelhouse tool: TLS verification failed.")
    if result.returncode == 22 and status:
        raise ValueError(f"Cannot acquire the wheelhouse tool: HTTP {status}.")
    raise ValueError(f"Cannot acquire the wheelhouse tool: downloader exited with status {result.returncode}.")


def ensure_tool(pool: Path, interface: str | None, *, name: str = TOOL_NAME,
                expected: str = TOOL_SHA256, download=_download) -> bool:
    """Return false only when the tool is absent and no network route was supplied."""
    if interface is not None and interface != "system" and (
        interface in (".", "..") or re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface) is None
    ):
        raise ValueError("The selected network interface is invalid.")
    directory = os.open(pool, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _lock(directory, fcntl.LOCK_SH)
        if _present(directory, name, expected):
            return True
        if interface is None:
            return False
        fcntl.flock(directory, fcntl.LOCK_UN)
        _lock(directory, fcntl.LOCK_EX)
        if _present(directory, name, expected):
            return True
        for attempt in range(MAX_ATTEMPTS):
            temporary = f".wheelhouse-tool-{secrets.token_hex(16)}.part"
            descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            transient = None
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
            except TransientToolDownload as error:
                transient = error
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError as error:
                    raise OSError(f"Cannot remove incomplete wheelhouse tool file {temporary}; it was preserved.") from error
            if transient is None:
                return True
            if attempt + 1 == MAX_ATTEMPTS:
                raise ValueError(f"Cannot acquire the wheelhouse tool after {MAX_ATTEMPTS} attempts: {transient}.") from None
            time.sleep(max(2**attempt, transient.retry_after))
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
        raise ToolInterrupted("The shared wheelhouse tool operation was interrupted.")

    previous = {number: signal.signal(number, interrupted) for number in signals}
    try:
        return 0 if ensure_tool(args.pool, args.interface) else 10
    except (OSError, ValueError, ToolInterrupted) as error:
        parser.exit(1, f"{error}\n")
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
