"""Probe policy, retirement state and failure diagnostics."""

from dataclasses import asdict, dataclass
from enum import StrEnum
import re
from typing import Protocol

from .core import Identifier


class Cancellation(Protocol):
    """A cancellation request sampled by the operation and its command transport."""

    def is_set(self) -> bool: ...


class StepError(RuntimeError):
    """An authored, credential-free explanation suitable for operator output."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Bounds:
    """Finite operation/command/cleanup deadlines and the probe lifetime, in seconds."""

    operation_seconds: float = 30
    command_seconds: float = 5
    cleanup_seconds: float = 15
    lifetime_seconds: int = 90

    def __post_init__(self) -> None:
        for value in (self.operation_seconds, self.command_seconds, self.cleanup_seconds):
            if type(value) not in (int, float) or not 0 < value <= 3600:
                raise StepError("invalid-policy", "time bounds must be finite and between zero and 3600 seconds")
        if type(self.lifetime_seconds) is not int or not 1 <= self.lifetime_seconds <= 3600:
            raise StepError("invalid-policy", "probe lifetime must be an integer between 1 and 3600 seconds")


@dataclass(frozen=True)
class ProbeRequest:
    """Explicit local-engine policy; invalid values raise StepError at construction."""

    endpoint: str
    image: str
    interpreter: str
    user_uid: int
    user_gid: int
    userns_mode: str = "default"
    bounds: Bounds = Bounds()

    def __post_init__(self) -> None:
        if type(self.bounds) is not Bounds:
            raise StepError("invalid-policy", "invalid operation bounds")
        if (not isinstance(self.endpoint, str) or not self.endpoint.startswith("unix:///")
                or len(self.endpoint) > 4096 or self.endpoint != self.endpoint.strip()
                or "?" in self.endpoint or "#" in self.endpoint
                or any(ord(c) < 32 or ord(c) == 127 for c in self.endpoint)):
            raise StepError("invalid-policy", "use the caller-selected local Unix Docker endpoint")
        if (not isinstance(self.image, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,511}", self.image) is None):
            raise StepError("invalid-policy", "image must be an explicit bounded identifier")
        try:
            object.__setattr__(self, "user_uid", Identifier(self.user_uid))
            object.__setattr__(self, "user_gid", Identifier(self.user_gid))
        except ValueError as cause:
            raise StepError("invalid-policy", "IDs must be integers from 0 through 4294967294") from cause
        if self.userns_mode not in ("default", "host"):
            raise StepError("invalid-policy", "user namespace policy must be default or host")
        if (not isinstance(self.interpreter, str) or not self.interpreter.startswith("/")
                or len(self.interpreter) > 512
                or any(ord(c) < 32 or ord(c) == 127 for c in self.interpreter)):
            raise StepError("invalid-policy", "supply an absolute Python interpreter path in the probe image")


class ResourceState(StrEnum):
    NOT_CREATED = "known-not-created"
    UNRESOLVED = "uncertain-outcome"
    RETIRED = "confirmed-retired"


@dataclass(frozen=True)
class Problem:
    category: str
    message: str


@dataclass(frozen=True)
class Retirement:
    state: ResourceState
    container_id: str | None = None
    problems: tuple[Problem, ...] = ()


@dataclass(frozen=True)
class ProbeHandle:
    """Engine and resource references for investigating an unresolved operation."""

    endpoint: str
    operation_token: str
    container_id: str | None = None
    daemon_id: str | None = None


class ProbeFailure(RuntimeError):
    """An operation failure with retirement state and ordered diagnostics."""

    def __init__(self, problem: Problem, retirement: Retirement, handle: ProbeHandle | None = None):
        super().__init__(problem.message)
        self.problem = problem
        self.retirement = retirement
        self.handle = handle

    def as_dict(self) -> dict:
        # silent-defaults: allow-contract -- A missing diagnostic handle is represented as JSON null, never synthesized ownership evidence; tests/test_cli.py::CliTests.
        return {"ok": False, "problem": asdict(self.problem), **asdict(self.retirement),
                "handle": None if self.handle is None else asdict(self.handle)}
