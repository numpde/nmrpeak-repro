"""Read host-relative maps with process and reader checks."""

from dataclasses import dataclass

from .core import IdMap
from ._procfs import qualified_process, ProcessIdentity
from ._errors import IdentityError

__all__ = ["observe_host_process", "ProcessObservation", "ProcessIdentity", "IdentityError"]


@dataclass(frozen=True)
class ProcessObservation:
    """Reusable maps and the process identity checked while reading them."""

    identity: ProcessIdentity
    uid_map: IdMap
    gid_map: IdMap


def observe_host_process(pid: int) -> ProcessObservation:
    """Return maps only after host, process and map continuity are confirmed.

    The reader must be in the initial user namespace; PID is interpreted in
    its own visible procfs. The process identity and map rows are checked
    before returning the observation.
    """
    with qualified_process(pid) as process:
        maps = process.read_maps()
        return ProcessObservation(process.identity, IdMap(maps.uid_rows), IdMap(maps.gid_rows))
