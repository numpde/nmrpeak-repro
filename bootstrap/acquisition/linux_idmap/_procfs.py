"""Check host coordinates and read from pinned process directories."""

from __future__ import annotations

import ctypes
import fcntl
import os
import re
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ._errors import IdentityError
from ._files import FileIdentity
from .core import MapRows, read_maps

# Linux nsfs ABI identity, checked before namespace ioctls.
# repo-check: allow-module-constant
_NSFS_MAGIC = 0x6E736673
# Linux procfs ABI identity, checked before process reads.
# repo-check: allow-module-constant
_PROCFS_MAGIC = 0x9FA0
# Linux UAPI initial user-namespace inode.
# repo-check: allow-module-constant
_USER_INIT_INO = 4026531837
# Linux nsfs ioctl ABI for namespace-type evidence.
# repo-check: allow-module-constant
_NS_GET_NSTYPE = 0xB703
# Canonical process-stat decimal grammar.
# repo-check: allow-module-constant
_DECIMAL = re.compile(rb"(?:0|[1-9][0-9]*)\Z")
# Linux proc stat states accepted as live process evidence.
# repo-check: allow-module-constant
_LIVE_STATES = frozenset("RSDTtWIKP")
# Linux proc stat states that establish process exit.
# repo-check: allow-module-constant
_DEAD_STATES = frozenset("ZXx")


@dataclass(frozen=True)
class ReaderEvidence:
    user_namespace_inode: int
    procfs: FileIdentity
    reader_pid: int
    reader_tid: int


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    starttime: int
    directory: FileIdentity


def _filesystem_type(fd: int) -> int:
    # Linux struct statfs starts with a native long f_type. An aligned oversized
    # buffer avoids architecture-dependent layouts of the remaining fields.
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.fstatfs
    call.argtypes = [ctypes.c_int, ctypes.c_void_p]
    call.restype = ctypes.c_int
    buffer = (ctypes.c_long * 64)()
    if call(fd, ctypes.byref(buffer)) != 0:
        raise OSError(ctypes.get_errno(), "filesystem identity unavailable")
    return buffer[0]


@contextmanager
def _directory(path, *, dir_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        if _filesystem_type(fd) != _PROCFS_MAGIC:
            raise IdentityError("host-precondition", "Map observation requires genuine procfs")
        yield fd
    finally:
        os.close(fd)


def _namespace_inode(root_fd: int, name: str) -> int:
    fd = os.open(f"thread-self/ns/{name}", os.O_RDONLY | os.O_CLOEXEC, dir_fd=root_fd)
    try:
        expected_type = {"user": 0x10000000, "pid": 0x20000000}[name]
        if _filesystem_type(fd) != _NSFS_MAGIC or fcntl.ioctl(fd, _NS_GET_NSTYPE) != expected_type:
            raise IdentityError("host-precondition", "Namespace evidence is not the required nsfs type")
        return os.fstat(fd).st_ino
    finally:
        os.close(fd)


def _read_at(fd: int, name: str, limit: int) -> bytes:
    source = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
    try:
        if _filesystem_type(source) != _PROCFS_MAGIC:
            raise IdentityError("process-identity", "Process evidence is not genuine procfs")
        raw = bytearray()
        while len(raw) <= limit:
            part = os.read(source, limit + 1 - len(raw))
            if not part:
                return bytes(raw)
            raw.extend(part)
        raise IdentityError("process-identity", "Process evidence exceeds its read bound")
    finally:
        os.close(source)


@dataclass(frozen=True)
class ProcessStatus:
    """The liveness and start time reported by one /proc/PID/stat read."""

    state: str
    starttime: int

    @property
    def live(self) -> bool:
        return self.state in _LIVE_STATES

    @property
    def exited(self) -> bool:
        return self.state in _DEAD_STATES

    @classmethod
    def of(cls, raw: bytes, pid: int) -> ProcessStatus:
        # comm is parenthesized but may itself contain spaces and parentheses.
        closing = raw.rfind(b") ")
        if not raw.startswith(str(pid).encode("ascii") + b" (") or closing < 0:
            raise IdentityError("process-identity", "Process status identity is malformed")
        fields = raw[closing + 2 :].split()
        # After comm, state is field 3 and starttime is field 22 (offset 19).
        if (
            len(fields) < 20
            or fields[0] not in {state.encode("ascii") for state in _LIVE_STATES | _DEAD_STATES}
            or _DECIMAL.fullmatch(fields[19]) is None
            or len(fields[19]) > 20
        ):
            raise IdentityError("process-identity", "Process status fields are malformed")
        starttime = int(fields[19])
        if starttime > 0xFFFFFFFFFFFFFFFF:
            raise IdentityError("process-identity", "Process start time overflows")
        return cls(fields[0].decode("ascii"), starttime)


def _validate_pid(pid: int):
    if type(pid) is not int or not 1 <= pid <= 0x7FFFFFFF:
        raise IdentityError("process-identity", "Process has no valid visible PID")


class Procfs:
    """Read local procfs with filesystem and namespace checks."""

    # silent-defaults: allow-contract -- Linux process evidence comes from /proc unless a caller explicitly supplies a root; filesystem and namespace checks apply equally to both; tests/test_host_addon.py::HostGuardTests.
    def __init__(self, proc_root: Path = Path("/proc")):
        self.proc_root = Path(proc_root)

    def reader_evidence(self) -> ReaderEvidence:
        if sys.platform != "linux":
            raise IdentityError("host-precondition", "Process map observation requires Linux")
        try:
            with _directory(self.proc_root) as root:
                pid, tid = os.getpid(), threading.get_native_id()
                if os.readlink("self", dir_fd=root) != str(pid) or os.readlink("thread-self", dir_fd=root) != f"{pid}/task/{tid}":
                    raise IdentityError("host-precondition", "procfs does not expose the calling process")
                user = _namespace_inode(root, "user")
                return ReaderEvidence(user, FileIdentity.of(os.fstat(root)), pid, tid)
        except OSError as cause:
            raise IdentityError("host-precondition", "Reader namespace evidence is unavailable") from cause

    @contextmanager
    def root(self):
        """Keep each successful process read bound to the same reader procfs."""
        host = self.reader_evidence()
        with _directory(self.proc_root) as root:
            if FileIdentity.of(os.fstat(root)) != host.procfs:
                raise IdentityError("host-precondition", "procfs changed before process evidence read")
            yield root, host
        if host != self.reader_evidence():
            raise IdentityError("host-precondition", "Reader namespace evidence changed during process evidence read")

    @contextmanager
    def process(self, pid: int):
        """Yield a pinned live process and check its identity again on successful exit."""
        _validate_pid(pid)
        try:
            with self.root() as (root, reader), _directory(str(pid), dir_fd=root) as fd:
                metadata = FileIdentity.of(os.fstat(fd))
                status = ProcessStatus.of(_read_at(fd, "stat", 4096), pid)
                if not status.live:
                    raise IdentityError("process-identity", "Process is not live")
                identity = ProcessIdentity(pid, status.starttime, metadata)
                yield ProcessDirectory(fd, identity, reader)
                after_status = ProcessStatus.of(_read_at(fd, "stat", 4096), pid)
                after = FileIdentity.of(os.stat(str(pid), dir_fd=root, follow_symlinks=False))
                if not after_status.live or status.starttime != after_status.starttime or after != metadata:
                    raise IdentityError("process-identity", "Process was replaced or exited during observation")
        except OSError as cause:
            raise IdentityError("process-identity", "Cannot establish consistent process evidence") from cause


@dataclass(frozen=True)
class ProcessDirectory:
    """A bounded reader valid only inside Procfs.process()."""

    fd: int
    identity: ProcessIdentity
    reader: ReaderEvidence

    # silent-defaults: allow-contract -- Map files use a bounded read; other proc evidence supplies its own smaller bound.
    def read(self, name: str, limit: int = 65536) -> bytes:
        return _read_at(self.fd, name, limit)

    def read_maps(self):
        return consistent_maps(self.read)


def consistent_maps(read_file):
    """Compare complete reusable samples, consuming parse errors inside the scope."""
    try:
        # Consume both samples before comparison and before the source closes.
        def snapshot():
            maps = read_maps(read_file)
            return MapRows(list(maps.uid_rows), list(maps.gid_rows))

        first = snapshot()
        second = snapshot()
    except ValueError as cause:
        raise IdentityError("process-identity", "Process ID maps could not be interpreted") from cause
    if first != second:
        raise IdentityError("process-identity", "ID maps changed during observation")
    return first


def coherent_process(pid: int):
    """A genuine procfs source with repeated reader/process identity checks."""
    return Procfs().process(pid)


@contextmanager
def qualified_process(pid: int):
    """Refine the same pinned source to initial-user-namespace coordinates."""
    with coherent_process(pid) as source:
        require_host(source.reader)
        yield source


def require_host(reader: ReaderEvidence) -> None:
    if reader.user_namespace_inode != _USER_INIT_INO:
        raise IdentityError("host-precondition", "Map reader must be in the initial user namespace")
