"""Process ownership and retirement checks for the managed probe."""

import os
import re
from contextlib import contextmanager

from . import _procfs as _proc
from ._errors import IdentityError
from ._files import FileIdentity
from ._procfs import ProcessIdentity, ProcessStatus, Procfs, _directory, _validate_pid, require_host
from .core import IdMap
from .process import ProcessObservation


def _require_token(raw: bytes, token: str):
    if not raw.endswith(b"\0") or raw[:-1].split(b"\0").count(token.encode("ascii")) != 1:
        raise IdentityError("process-identity", "Probe process lacks its exact operation token")


# Docker reports host PIDs; a nested PID reader would inspect another process.
# repo-check: allow-module-constant
_HOST_PID_NAMESPACE = 4026531836


class ProbeProcesses(Procfs):
    def reader_evidence(self):
        host = super().reader_evidence()
        require_host(host)
        with _directory(self.proc_root) as root:
            if _proc._namespace_inode(root, "pid") != _HOST_PID_NAMESPACE:
                raise IdentityError("host-precondition", "Docker map reader must be in the initial PID namespace")
        return host

    def identify(self, pid: int, token: str) -> ProcessIdentity:
        with self._owned_process(pid, token) as process:
            return process.identity

    def observe(self, pid: int, token: str) -> ProcessObservation:
        with self._owned_process(pid, token) as process:
            maps = process.read_maps()
            return ProcessObservation(process.identity, IdMap(maps.uid_rows), IdMap(maps.gid_rows))

    @contextmanager
    def _owned_process(self, pid: int, token: str):
        """Check the token around reads from the same pinned process directory."""
        if type(token) is not str or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token) is None:
            raise IdentityError("process-identity", "Probe ownership token is invalid")
        with self.process(pid) as process:
            _require_token(process.read("cmdline", 8192), token)
            yield process
            _require_token(process.read("cmdline", 8192), token)

    def pid_absent(self, pid: int) -> bool:
        """Conservative retirement proof when an exact identity was unavailable."""
        _validate_pid(pid)
        try:
            with self.root() as (root, _reader):
                try:
                    os.stat(str(pid), dir_fd=root, follow_symlinks=False)
                # silent-defaults: allow-exception-outcome -- A missing proc entry establishes this PID is absent.
                except FileNotFoundError:
                    return True
                return False
        except OSError as cause:
            raise IdentityError("process-identity", "probe PID absence cannot be established") from cause

    def process_retired(self, observation: ProcessIdentity) -> bool:
        """Whether this exact process exited; unreadable evidence is not absence."""
        _validate_pid(observation.pid)
        try:
            with self.root() as (root, _reader):
                try:
                    with _directory(str(observation.pid), dir_fd=root) as process:
                        metadata = FileIdentity.of(os.fstat(process))
                        status = ProcessStatus.of(_proc._read_at(process, "stat", 4096), observation.pid)
                except FileNotFoundError:
                    retired = True
                else:
                    retired = status.exited or status.starttime != observation.starttime
                    if not retired and metadata != observation.directory:
                        # proc inode eviction can change inode numbers while a
                        # PID remains alive: a mismatch is not proof of exit.
                        raise IdentityError("process-identity", "Live process inode changed before retirement check")
            return retired
        except OSError as cause:
            raise IdentityError("process-identity", "Probe process retirement cannot be established") from cause
