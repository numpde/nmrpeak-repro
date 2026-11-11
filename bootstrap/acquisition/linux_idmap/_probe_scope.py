"""Ownership, checked observations and retirement for one image probe."""

from __future__ import annotations

import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass

from ._docker import Container, DockerEngine, DockerError
from ._errors import IdentityError
from ._probe_model import Cancellation, ProbeFailure, ProbeHandle, ProbeRequest, Problem, ResourceState, Retirement, StepError
from ._probe_process import ProbeProcesses
from ._probe_spec import ProbeSpec
from ._procfs import ProcessIdentity
from .process import ProcessObservation


class OwnedProbe:
    """Own a probe container and every process observed during its lifetime."""

    def __init__(self, engine: DockerEngine, guard: ProbeProcesses, request: ProbeRequest, budget: _Budget):
        self.engine = engine
        self.guard = guard
        self.request = request
        self.budget = budget
        self.spec: ProbeSpec | None = None
        self.state = ResourceState.NOT_CREATED
        self.cid: str | None = None
        self.processes: dict[ProcessIdentity, None] = {}
        # None means this reported PID still needs an identity or an absence check.
        self.reported_runs: dict[ContainerRun, ProcessIdentity | None] = {}
        self.run: ContainerRun | None = None

    def create(self) -> None:
        """Resolve the local image and create its finite Python probe."""
        image_id = self.engine.image_id(self.request.image, **self.budget.arguments())
        self.spec = ProbeSpec(secrets.token_hex(16), image_id, self.request)
        command = self.spec.create_arguments()
        arguments = self.budget.arguments()
        self.state = ResourceState.UNRESOLVED  # A lost client reply cannot prove non-creation.
        try:
            self.cid = self.engine.create(command, **arguments)
        except (DockerError, BaseExceptionGroup) as error:
            # Missing dispatch metadata cannot prove non-creation for a foreign group.
            if not getattr(error, "mutation_uncertain", True):
                self.state = ResourceState.NOT_CREATED
            raise

    def start(self) -> None:
        if self.inspect(self.budget) is None:
            raise StepError("process-identity", "created probe disappeared before start")
        self.engine.start(self.cid, **self.budget.arguments())

    def observe(self) -> ProcessObservation:
        """Read matching maps from two observations of the same running probe."""
        first = self.inspect(self.budget)
        if first is None or not first.running or type(first.pid) is not int or first.pid <= 0:
            raise StepError("process-identity", "started probe is not running")
        run = ContainerRun.of(first)
        observation = self.sample(first)
        second = self.inspect(self.budget)
        if second is None or not second.running or ContainerRun.of(second) != run:
            raise StepError("process-identity", "probe run changed during observation")
        if self.sample(second) != observation:
            raise StepError("process-identity", "probe process or maps changed during observation")
        self.budget.remaining()
        self.run = run
        return observation

    def sample(self, container: Container) -> ProcessObservation:
        observation = self.guard.observe(container.pid, self.spec.token)
        self.record_process(ContainerRun.of(container), observation.identity)
        return observation

    def inspect(self, budget: _Budget) -> Container | None:
        container = self.engine.inspect(self.cid, **budget.arguments())
        if container is not None:
            self.spec.verify(container, self.cid)
            if container.running or container.pid > 0:
                # silent-defaults: allow-contract -- A newly reported run has no checked process identity yet.
                self.reported_runs.setdefault(ContainerRun.of(container), None)
        return container

    def record_process(self, run: ContainerRun, identity: ProcessIdentity) -> None:
        # A conflicting second sample must not overwrite the first obligation.
        self.processes[identity] = None
        self.reported_runs[run] = identity
        # At one PID, an earlier process either persisted or exited before reuse.
        for reported, known in self.reported_runs.items():
            if known is None and reported.pid == identity.pid:
                self.reported_runs[reported] = identity

    def run_changed(self, container: Container | None) -> bool:
        # Normal stopped/PID0 is allowed, but a different generation is not.
        return (
            self.run is not None
            and container is not None
            and (
                container.started_at != self.run.started_at
                or (container.running and container.pid != self.run.pid)
                or (not container.running and container.pid != 0)
            )
        )

    def handle(self) -> ProbeHandle | None:
        if self.spec is None:
            return None
        return ProbeHandle(
            endpoint=self.engine.identity.endpoint,
            operation_token=self.spec.token,
            container_id=self.cid,
            daemon_id=self.engine.identity.info.daemon_id,
        )

    def retire(self) -> list[BaseException]:
        """Retire the owned resource; retain failures in the order they occurred."""
        if self.state == ResourceState.NOT_CREATED:
            return []
        run_error = identity_error = removal_error = polling_error = confirmation_error = None
        try:
            bounds = self.request.bounds
            budget = _Budget(bounds.cleanup_seconds, bounds.command_seconds)
            if self.cid is None:
                candidates = self.engine.candidates(self.spec.token, **budget.arguments())
                if len(candidates) != 1:
                    raise StepError("reconciliation", "creation remains unresolved; no unique owned candidate")
                self.cid = candidates[0]
            container = self.inspect(budget)
            # Completed maps survive normal exit. The mandatory inspection can
            # still expose a replacement run or a contradictory reported PID.
            if self.run_changed(container):
                run_error = StepError("process-identity", "probe run changed after acquisition")
            if container is not None and (container.running or container.pid > 0):
                try:
                    current = self.guard.identify(container.pid, self.spec.token)
                    if run_error is None and self.run is not None and current not in self.processes:
                        run_error = StepError("process-identity", "probe process identity changed after acquisition")
                    self.record_process(ContainerRun.of(container), current)
                except BaseException as error:
                    identity_error = error
            if container is not None:
                try:
                    self.engine.remove(container.cid, **budget.arguments())
                except BaseException as error:
                    removal_error = error
                while (reported := self.inspect(budget)) is not None:
                    if polling_error is None and self.run_changed(reported):
                        polling_error = StepError("process-identity", "probe run changed during removal")
                    budget.pause()
            for identity in self.processes:
                while not self.guard.process_retired(identity):
                    budget.pause()
            for run, identity in self.reported_runs.items():
                if identity is None:
                    while not self.guard.pid_absent(run.pid):
                        budget.pause()
            self.engine.assert_unchanged(**budget.arguments())
            self.state = ResourceState.RETIRED
            # Lost/nonzero remove replies are settled by the checks above.
            # Safety, cancellation, and client-cleanup failures are never erased.
            if isinstance(removal_error, DockerError) and removal_error.category in {"timeout", "docker_command"}:
                removal_error = None
        except BaseException as error:
            confirmation_error = error
        return [
            error for error in (run_error, identity_error, removal_error, polling_error, confirmation_error) if error is not None
        ]


@dataclass(frozen=True)
class ContainerRun:
    """Container execution reported by inspection; liveness is checked separately."""

    container_id: str
    pid: int
    started_at: str

    @classmethod
    def of(cls, container: Container) -> ContainerRun:
        return cls(container.cid, container.pid, container.started_at)


class _Budget:
    def __init__(self, seconds: float, command_seconds: float, cancel=None):
        self.deadline = time.monotonic() + seconds
        self.command_seconds = command_seconds
        self.cancel = cancel

    def remaining(self) -> float:
        if self.cancel is not None and self.cancel.is_set():
            raise StepError("cancelled", "operation was cancelled")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise StepError("timeout", "operation time bound expired")
        return left

    def arguments(self) -> dict:
        return {"timeout": min(self.command_seconds, self.remaining()), "cancel": self.cancel}

    def pause(self):
        time.sleep(min(0.05, self.remaining()))


def _problem(error: BaseException) -> Problem:
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return Problem("cancelled", "operation was interrupted")
    if isinstance(error, (DockerError, IdentityError, StepError)):
        return Problem(error.category, str(error))
    if isinstance(error, OSError):
        return Problem("local-io", "local operation failed; inspect the probe handle")
    return Problem("internal", "unexpected operation failure; inspect the probe handle")


def _errors(error: BaseException | None):
    """Yield the leaf failures in execution order."""
    if isinstance(error, BaseExceptionGroup):
        for cause in error.exceptions:
            yield from _errors(cause)
    elif error is not None:
        yield error


@contextmanager
def owned_probe(request: ProbeRequest, *, cancel: Cancellation | None, executable: str):
    """Connect the selected engine and own the probe from acquisition through cleanup.

    Successful exit confirms probe retirement and closes the client.
    """
    engine = probe = None
    operation_error = close_error = None
    retirement_errors = []
    try:
        bounds = request.bounds
        budget = _Budget(bounds.operation_seconds, bounds.command_seconds, cancel)
        budget.remaining()
        guard = ProbeProcesses()
        reader = guard.reader_evidence()
        engine = DockerEngine.connect(request.endpoint, executable=executable, **budget.arguments())
        probe = OwnedProbe(engine, guard, request, budget)
        yield probe
        if guard.reader_evidence() != reader:
            raise StepError("host-precondition", "reader namespace evidence changed")
    except BaseException as error:
        operation_error = error
    finally:
        if probe is not None:
            try:
                retirement_errors = probe.retire()
            except BaseException as error:
                # Preserve the owner's state when retirement itself is interrupted.
                retirement_errors = [error]
        if engine is not None:
            try:
                engine.close()
            except BaseException as error:
                close_error = error
    failures = [cause for error in (operation_error, *retirement_errors, close_error) for cause in _errors(error)]
    late_cancel = None
    if cancel is not None and cancel.is_set() and not any(_problem(error).category == "cancelled" for error in failures):
        late_cancel = StepError("cancelled", "operation was cancelled before result delivery")
    failures = [error for error in (*failures, late_cancel) if error is not None]
    problems = [_problem(error) for error in failures]
    state = ResourceState.NOT_CREATED if probe is None else probe.state
    # silent-defaults: allow-contract -- No probe means no container identity exists.
    cid = None if probe is None else probe.cid
    # silent-defaults: allow-contract -- No probe means there is no owned resource handle.
    handle = None if probe is None else probe.handle()
    retirement = Retirement(state, cid, tuple(problems[1:]))
    if problems:
        raise ProbeFailure(problems[0], retirement, handle) from failures[0]
    if state != ResourceState.RETIRED:
        raise ProbeFailure(Problem("retirement", "probe retirement was not confirmed"), retirement, handle)
