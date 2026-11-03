"""Read host IDs from a temporary Python process in a local Docker image."""

from dataclasses import asdict, dataclass

from .core import IdMap
from ._probe_model import Cancellation, Bounds, Problem, ProbeFailure, ProbeHandle, ProbeRequest, ResourceState, Retirement, StepError
from ._probe_scope import owned_probe

__all__ = ["probe_identity", "ProbeRequest", "Bounds", "ProbeResult", "StepError",
           "ProbeFailure", "ResourceState", "Problem", "Retirement", "ProbeHandle"]


@dataclass(frozen=True)
class ProbeResult:
    uid_map: IdMap
    gid_map: IdMap
    container_id: str

    def as_dict(self) -> dict:
        return {"ok": True, "uid_map": asdict(self.uid_map)["rows"],
                "gid_map": asdict(self.gid_map)["rows"], "container_id": self.container_id,
                "state": ResourceState.RETIRED}


# silent-defaults: allow-contract -- The Docker CLI is used unless the caller selects another executable; tests/test_probe_faults.py::OperationFaultTests.
def probe_identity(request: ProbeRequest, *, cancel: Cancellation | None = None,
                   executable: str = "docker") -> ProbeResult:
    """Create, start and observe a probe; return its maps after confirmed cleanup."""
    with owned_probe(request, cancel=cancel, executable=executable) as probe:
        probe.create()
        probe.start()
        observation = probe.observe()
    return ProbeResult(observation.uid_map, observation.gid_map, probe.cid)
