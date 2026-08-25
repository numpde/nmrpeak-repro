"""Own one admitted NMRPeak runner boot and its single in-flight request."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
import os
import secrets
import socket
import stat
from threading import Lock
import time
from typing import Generic, Protocol, TypeVar

from .canonical_json import JsonValue
from .runner_protocol import (
    AttemptCorrelation,
    RunnerProtocolError,
    GenerateFrame,
    ReadyFrame,
    RejectedFrame,
    ResultFrame,
    RetireFrame,
    RunnerFrameCodec,
    RunnerModelInput,
    ValidateFrame,
    ValidatedFrame,
)
from .owner_session_endpoint import open_owner_session_directory
from .product_result import NMRPEAK_SOURCE_CLOSURE_REF, ProviderResultFacts


ModelInput = TypeVar("ModelInput", bound=RunnerModelInput)


class _RunnerChannel(Protocol):
    def settimeout(self, value: float) -> None: ...
    def sendall(self, data: bytes) -> None: ...
    def recv_into(self, buffer: memoryview) -> int: ...
    def shutdown(self, how: int) -> None: ...
    def close(self) -> None: ...


class RunnerAdmissionError(RuntimeError):
    """A connected runner did not establish its provider-admitted boot."""


class RunnerSessionRetired(RuntimeError):
    """The runner boot is unusable and its channel has been closed."""


@dataclass(frozen=True, slots=True)
class RunnerDeadlines:
    """Bounded waits for each private runner exchange phase."""

    connect_seconds: float
    ready_seconds: float
    validate_seconds: float
    generate_seconds: float
    retire_seconds: float

    def __post_init__(self) -> None:
        for value in (
            self.connect_seconds,
            self.ready_seconds,
            self.validate_seconds,
            self.generate_seconds,
            self.retire_seconds,
        ):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("NMRPeak runner deadlines must be positive finite seconds")


@dataclass(frozen=True, slots=True)
class RunnerInputRejected:
    """The runner deterministically rejected a fully parsed model input."""

    message: str


@dataclass(frozen=True, slots=True)
class ValidatedRunnerRequest:
    """Capability to authorize generation for one validated runner request."""

    _owner: object = field(repr=False)
    _correlation: AttemptCorrelation = field(repr=False)


@dataclass(frozen=True, slots=True)
class GeneratedRunnerCandidates:
    """Untrusted output bound to the session request that generated it."""

    value: JsonValue
    _owner: object = field(repr=False, compare=False)
    _correlation: AttemptCorrelation = field(repr=False, compare=False)


class _SessionState(StrEnum):
    IDLE = "idle"
    VALIDATING = "validating"
    VALIDATED = "validated"
    GENERATING = "generating"
    RETIRED = "retired"


def open_runner_session(
    socket_path: str,
    facts: ProviderResultFacts,
    deadlines: RunnerDeadlines,
    codec: RunnerFrameCodec[ModelInput],
) -> RunnerSession[ModelInput]:
    """Attach to one private endpoint, then admit its concrete READY exchange."""

    if type(deadlines) is not RunnerDeadlines:
        raise TypeError("NMRPeak runner attachment requires owned deadlines")
    lane = codec.lane_name
    connect_deadline = time.monotonic() + deadlines.connect_seconds
    parent_fd, socket_name = open_owner_session_directory(socket_path)
    try:
        while True:
            remaining = _remaining_connect_seconds(connect_deadline)
            try:
                endpoint = os.stat(
                    socket_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                time.sleep(min(0.01, remaining))
                continue
            if not stat.S_ISSOCK(endpoint.st_mode):
                raise RunnerAdmissionError(
                    f"Cannot connect to {lane} runner: endpoint is not a Unix socket"
                )
            if (
                endpoint.st_uid != os.geteuid()
                or stat.S_IMODE(endpoint.st_mode) != 0o600
            ):
                raise RunnerAdmissionError(
                    f"Cannot connect to {lane} runner: endpoint is not owner-only"
                )

            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.settimeout(remaining)
                connection.connect(socket_path)
            except (FileNotFoundError, ConnectionRefusedError):
                close_error = _close_channel(connection)
                if close_error is not None:
                    raise RunnerAdmissionError(
                        f"Cannot retry {lane} runner connection after socket cleanup failed"
                    ) from close_error
                time.sleep(min(0.01, _remaining_connect_seconds(connect_deadline)))
                continue
            except BaseException as error:
                close_error = _close_channel(connection)
                if close_error is not None:
                    error.add_note(f"The failed {lane} connection also failed to close.")
                raise

            try:
                return RunnerSession.admit(connection, facts, deadlines, codec)
            except BaseException:
                _close_channel(connection)
                raise
    finally:
        os.close(parent_fd)


class RunnerSession(Generic[ModelInput]):
    """Serialize one provider owner session against one admitted runner boot."""

    def __init__(
        self,
        channel: _RunnerChannel,
        facts: ProviderResultFacts,
        deadlines: RunnerDeadlines,
        codec: RunnerFrameCodec[ModelInput],
        boot_generation: str,
    ) -> None:
        self._channel = channel
        self._facts = facts
        self._deadlines = deadlines
        self._codec = codec
        self._boot_generation = boot_generation
        self._state = _SessionState.IDLE
        self._pending: ValidatedRunnerRequest | None = None
        self._lock = Lock()

    @classmethod
    def admit(
        cls,
        channel: _RunnerChannel,
        facts: ProviderResultFacts,
        deadlines: RunnerDeadlines,
        codec: RunnerFrameCodec[ModelInput],
    ) -> RunnerSession[ModelInput]:
        """Receive READY and compare every echo with provider-admitted facts."""

        if type(facts) is not ProviderResultFacts:
            raise TypeError("NMRPeak runner admission requires provider-owned result facts")
        if type(deadlines) is not RunnerDeadlines:
            raise TypeError("NMRPeak runner admission requires owned deadlines")
        lane = codec.lane_name
        try:
            channel.settimeout(deadlines.ready_seconds)
            frame = codec.receive(channel)
            if type(frame) is not ReadyFrame:
                raise RunnerProtocolError(
                    f"Cannot admit {lane} runner: the first frame is not READY"
                )
            if not _ready_matches_provider_facts(frame, facts):
                raise RunnerProtocolError(
                    f"Cannot admit {lane} runner: READY facts differ from the deployment"
                )
        except (OSError, RunnerProtocolError) as error:
            admission_error = RunnerAdmissionError(
                f"Cannot admit {lane} runner boot from its READY exchange"
            )
            close_error = _close_channel(channel)
            if close_error is not None:
                admission_error.add_note(
                    "The rejected runner channel also failed to close."
                )
            raise admission_error from error
        return cls(channel, facts, deadlines, codec, frame.boot_generation)

    @property
    def result_facts(self) -> ProviderResultFacts:
        """Return the provider authority retained independently of READY echoes."""

        return self._facts

    @property
    def retired(self) -> bool:
        """Report whether this admitted boot can no longer accept work."""

        with self._lock:
            return self._state is _SessionState.RETIRED

    def validate(
        self,
        *,
        execution_attempt_ref: str,
        provider_attempt_key: str,
        model_input: ModelInput,
    ) -> ValidatedRunnerRequest | RunnerInputRejected:
        """Ask the runner to tokenize one input without executing the model."""

        correlation = AttemptCorrelation(
            boot_generation=self._boot_generation,
            correlation_id="request:" + secrets.token_hex(16),
            attempt_ref=execution_attempt_ref,
            provider_attempt_key=provider_attempt_key,
        )
        frame = ValidateFrame(correlation, model_input)
        with self._lock:
            wrong_state = self._state is not _SessionState.IDLE
            if not wrong_state:
                self._state = _SessionState.VALIDATING
        if wrong_state:
            self._retire_with_error(
                f"Cannot validate {self._codec.lane_name} runner input: the session is not idle"
            )

        response = self._exchange(
            frame,
            self._deadlines.validate_seconds,
            f"validate {self._codec.lane_name} runner input",
        )
        if type(response) is ValidatedFrame and response.correlation == correlation:
            request = ValidatedRunnerRequest(self, correlation)
            with self._lock:
                if self._state is not _SessionState.VALIDATING:
                    raise RunnerSessionRetired(
                        f"Cannot accept {self._codec.lane_name} validation: the session was retired"
                    )
                self._pending = request
                self._state = _SessionState.VALIDATED
            return request
        if type(response) is RejectedFrame and response.correlation == correlation:
            with self._lock:
                if self._state is not _SessionState.VALIDATING:
                    raise RunnerSessionRetired(
                        f"Cannot accept {self._codec.lane_name} rejection: the session was retired"
                    )
                self._state = _SessionState.IDLE
            return RunnerInputRejected(response.diagnostic)
        self._retire_with_error(
            f"Cannot validate {self._codec.lane_name} runner input: "
            "response type or correlation is wrong"
        )

    def generate(self, request: ValidatedRunnerRequest) -> GeneratedRunnerCandidates:
        """Authorize model execution for the exact validated request."""

        with self._lock:
            wrong_request = (
                self._state is not _SessionState.VALIDATED
                or request is not self._pending
                or request._owner is not self
            )
            if not wrong_request:
                self._state = _SessionState.GENERATING
        if wrong_request:
            self._retire_with_error(
                f"Cannot generate {self._codec.lane_name} candidates: "
                "validated request is not current"
            )

        response = self._exchange(
            GenerateFrame(request._correlation),
            self._deadlines.generate_seconds,
            f"generate {self._codec.lane_name} candidates",
        )
        if type(response) is ResultFrame and response.correlation == request._correlation:
            with self._lock:
                if self._state is not _SessionState.GENERATING:
                    raise RunnerSessionRetired(
                        f"Cannot accept {self._codec.lane_name} result: the session was retired"
                    )
                self._pending = None
                self._state = _SessionState.IDLE
            return GeneratedRunnerCandidates(response.candidates, self, request._correlation)
        self._retire_with_error(
            f"Cannot generate {self._codec.lane_name} candidates: "
            "response type or correlation is wrong"
        )

    def candidates_for_attempt(
        self,
        generated: GeneratedRunnerCandidates,
        *,
        execution_attempt_ref: str,
        provider_attempt_key: str,
    ) -> JsonValue:
        """Bind generated output to the retained Attempt facts that may consume it."""

        if (
            type(generated) is not GeneratedRunnerCandidates
            or generated._owner is not self
            or generated._correlation.attempt_ref != execution_attempt_ref
            or generated._correlation.provider_attempt_key != provider_attempt_key
        ):
            raise ValueError(
                f"{self._codec.lane_name} generated candidates do not belong "
                "to this retained Attempt"
            )
        return generated.value

    def cancel(self) -> None:
        """Retire the boot immediately so a blocked exchange cannot continue."""

        with self._lock:
            if self._state is _SessionState.RETIRED:
                return
            self._state = _SessionState.RETIRED
            self._pending = None
        close_error = _close_channel(self._channel)
        if close_error is not None:
            raise RunnerSessionRetired(
                f"{self._codec.lane_name} cancellation retired the session, "
                "but channel closure failed"
            ) from close_error

    def retire(self) -> None:
        """Hand off idle RETIRE and close the provider side without claiming exit."""

        with self._lock:
            wrong_state = self._state is not _SessionState.IDLE
            if not wrong_state:
                self._state = _SessionState.RETIRED
        if wrong_state:
            self._retire_with_error(
                f"Cannot gracefully retire {self._codec.lane_name} runner boot: "
                "the session is not idle"
            )
        try:
            self._channel.settimeout(self._deadlines.retire_seconds)
            self._channel.sendall(
                self._codec.encode(RetireFrame(self._boot_generation))
            )
        except (OSError, RunnerProtocolError, TypeError, ValueError) as error:
            retirement_error = RunnerSessionRetired(
                f"Cannot determine whether idle {self._codec.lane_name} RETIRE was handed off; "
                "the provider session is retired"
            )
            close_error = _close_channel(self._channel)
            if close_error is not None:
                retirement_error.add_note(
                    "The retired runner channel also failed to close."
                )
            raise retirement_error from error
        close_error = _close_channel(self._channel)
        if close_error is not None:
            raise RunnerSessionRetired(
                f"{self._codec.lane_name} RETIRE was handed off, "
                "but provider channel closure failed"
            ) from close_error

    def _exchange(
        self,
        request: ValidateFrame | GenerateFrame,
        timeout_seconds: float,
        operation: str,
    ) -> object:
        try:
            self._channel.settimeout(timeout_seconds)
            self._channel.sendall(self._codec.encode(request))
            return self._codec.receive(self._channel)
        except (OSError, RunnerProtocolError, TypeError, ValueError) as error:
            self._retire_with_error(
                f"Cannot {operation}: the runner exchange failed",
                cause=error,
            )

    def _retire_with_error(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        with self._lock:
            self._state = _SessionState.RETIRED
            self._pending = None
        error = RunnerSessionRetired(message)
        close_error = _close_channel(self._channel)
        if close_error is not None:
            error.add_note("The retired runner channel also failed to close.")
        if cause is None:
            raise error
        raise error from cause


def _ready_matches_provider_facts(
    ready: ReadyFrame,
    facts: ProviderResultFacts,
) -> bool:
    return (
        ready.runner_ref == facts.identity.runner_ref
        and ready.runner_contract_id == facts.runner_contract_id
        and ready.release_sha256 == facts.checkpoint_ref
        and ready.source_closure_sha256 == NMRPEAK_SOURCE_CLOSURE_REF
        and ready.image_input_id == facts.image_input_ref
        and ready.target == "cpu-x86_64"
        and ready.device == "cpu"
        and ready.decode_policy_id == facts.identity.decode_policy.decode_policy_id
    )


def _remaining_connect_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RunnerAdmissionError(
            "Cannot connect to NMRPeak runner before the connect deadline"
        )
    return remaining


def _close_channel(channel: _RunnerChannel) -> OSError | None:
    try:
        channel.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        channel.close()
    except OSError as error:
        return error
    return None
