"""Own one admitted CHF runner boot and its single in-flight request."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
import secrets
import socket
from threading import Lock
from typing import Protocol

from .canonical_json import JsonValue
from .chf_binding import ChfRunnerInput
from .chf_runner_protocol import (
    AttemptCorrelation,
    ChfRunnerProtocolError,
    GenerateFrame,
    ReadyFrame,
    RejectedFrame,
    ResultFrame,
    RetireFrame,
    ValidateFrame,
    ValidatedFrame,
    encode_chf_runner_frame,
    receive_chf_runner_frame,
)
from .product_result import (
    CHF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
    ProviderResultFacts,
)


class _RunnerChannel(Protocol):
    def settimeout(self, value: float) -> None: ...
    def sendall(self, data: bytes) -> None: ...
    def recv_into(self, buffer: memoryview) -> int: ...
    def shutdown(self, how: int) -> None: ...
    def close(self) -> None: ...


class ChfRunnerAdmissionError(RuntimeError):
    """A connected runner did not establish the admitted CHF boot."""


class ChfRunnerSessionRetired(RuntimeError):
    """The CHF boot is unusable and its channel has been closed."""


@dataclass(frozen=True, slots=True)
class ChfRunnerDeadlines:
    """Bounded waits for each private runner exchange phase."""

    ready_seconds: float
    validate_seconds: float
    generate_seconds: float
    retire_seconds: float

    def __post_init__(self) -> None:
        for value in (
            self.ready_seconds,
            self.validate_seconds,
            self.generate_seconds,
            self.retire_seconds,
        ):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("CHF runner deadlines must be positive finite seconds")


@dataclass(frozen=True, slots=True)
class ChfInputRejected:
    """The runner deterministically rejected a fully parsed model input."""


@dataclass(frozen=True, slots=True)
class ValidatedChfRequest:
    """Capability to authorize generation for one validated runner request."""

    _owner: object = field(repr=False)
    _correlation: AttemptCorrelation = field(repr=False)


@dataclass(frozen=True, slots=True)
class UntrustedChfCandidates:
    """Runner output awaiting the product result validator."""

    value: JsonValue


class _SessionState(StrEnum):
    IDLE = "idle"
    VALIDATING = "validating"
    VALIDATED = "validated"
    GENERATING = "generating"
    RETIRED = "retired"


class ChfRunnerSession:
    """Serialize one provider owner session against one admitted runner boot."""

    def __init__(
        self,
        channel: _RunnerChannel,
        facts: ProviderResultFacts,
        deadlines: ChfRunnerDeadlines,
        boot_generation: str,
    ) -> None:
        self._channel = channel
        self._facts = facts
        self._deadlines = deadlines
        self._boot_generation = boot_generation
        self._state = _SessionState.IDLE
        self._pending: ValidatedChfRequest | None = None
        self._lock = Lock()

    @classmethod
    def admit(
        cls,
        channel: _RunnerChannel,
        facts: ProviderResultFacts,
        deadlines: ChfRunnerDeadlines,
    ) -> ChfRunnerSession:
        """Receive READY and compare every echo with provider-admitted facts."""

        if type(facts) is not ProviderResultFacts:
            raise TypeError("CHF runner admission requires provider-owned result facts")
        if facts.identity is not CHF_RESULT_IDENTITY:
            raise AssertionError("CHF runner admission requires the CHF lane identity")
        if type(deadlines) is not ChfRunnerDeadlines:
            raise TypeError("CHF runner admission requires owned deadlines")
        try:
            channel.settimeout(deadlines.ready_seconds)
            frame = receive_chf_runner_frame(channel)
            if type(frame) is not ReadyFrame:
                raise ChfRunnerProtocolError(
                    "Cannot admit CHF runner: the first frame is not READY"
                )
            if not _ready_matches_provider_facts(frame, facts):
                raise ChfRunnerProtocolError(
                    "Cannot admit CHF runner: READY facts differ from the deployment"
                )
        except (OSError, ChfRunnerProtocolError) as error:
            admission_error = ChfRunnerAdmissionError(
                "Cannot admit CHF runner boot from its READY exchange"
            )
            close_error = _close_channel(channel)
            if close_error is not None:
                admission_error.add_note(
                    "The rejected runner channel also failed to close."
                )
            raise admission_error from error
        return cls(channel, facts, deadlines, frame.boot_generation)

    @property
    def result_facts(self) -> ProviderResultFacts:
        """Return the provider authority retained independently of READY echoes."""

        return self._facts

    def validate(
        self,
        *,
        execution_attempt_ref: str,
        provider_attempt_key: str,
        model_input: ChfRunnerInput,
    ) -> ValidatedChfRequest | ChfInputRejected:
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
                "Cannot validate CHF runner input: the session is not idle"
            )

        response = self._exchange(
            frame,
            self._deadlines.validate_seconds,
            "validate CHF runner input",
        )
        if type(response) is ValidatedFrame and response.correlation == correlation:
            request = ValidatedChfRequest(self, correlation)
            with self._lock:
                if self._state is not _SessionState.VALIDATING:
                    raise ChfRunnerSessionRetired(
                        "Cannot accept CHF validation: the session was retired"
                    )
                self._pending = request
                self._state = _SessionState.VALIDATED
            return request
        if type(response) is RejectedFrame and response.correlation == correlation:
            with self._lock:
                if self._state is not _SessionState.VALIDATING:
                    raise ChfRunnerSessionRetired(
                        "Cannot accept CHF rejection: the session was retired"
                    )
                self._state = _SessionState.IDLE
            return ChfInputRejected()
        self._retire_with_error(
            "Cannot validate CHF runner input: response type or correlation is wrong"
        )

    def generate(self, request: ValidatedChfRequest) -> UntrustedChfCandidates:
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
                "Cannot generate CHF candidates: validated request is not current"
            )

        response = self._exchange(
            GenerateFrame(request._correlation),
            self._deadlines.generate_seconds,
            "generate CHF candidates",
        )
        if type(response) is ResultFrame and response.correlation == request._correlation:
            with self._lock:
                if self._state is not _SessionState.GENERATING:
                    raise ChfRunnerSessionRetired(
                        "Cannot accept CHF result: the session was retired"
                    )
                self._pending = None
                self._state = _SessionState.IDLE
            return UntrustedChfCandidates(response.candidates)
        self._retire_with_error(
            "Cannot generate CHF candidates: response type or correlation is wrong"
        )

    def cancel(self) -> None:
        """Retire the boot immediately so a blocked exchange cannot continue."""

        with self._lock:
            if self._state is _SessionState.RETIRED:
                return
            self._state = _SessionState.RETIRED
            self._pending = None
        close_error = _close_channel(self._channel)
        if close_error is not None:
            raise ChfRunnerSessionRetired(
                "CHF cancellation retired the session, but channel closure failed"
            ) from close_error

    def retire(self) -> None:
        """Hand off idle RETIRE and close the provider side without claiming exit."""

        with self._lock:
            wrong_state = self._state is not _SessionState.IDLE
            if not wrong_state:
                self._state = _SessionState.RETIRED
        if wrong_state:
            self._retire_with_error(
                "Cannot gracefully retire CHF runner boot: the session is not idle"
            )
        try:
            self._channel.settimeout(self._deadlines.retire_seconds)
            self._channel.sendall(
                encode_chf_runner_frame(RetireFrame(self._boot_generation))
            )
        except (OSError, ChfRunnerProtocolError, TypeError, ValueError) as error:
            retirement_error = ChfRunnerSessionRetired(
                "Cannot determine whether idle CHF RETIRE was handed off; "
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
            raise ChfRunnerSessionRetired(
                "CHF RETIRE was handed off, but provider channel closure failed"
            ) from close_error

    def _exchange(
        self,
        request: ValidateFrame | GenerateFrame,
        timeout_seconds: float,
        operation: str,
    ) -> object:
        try:
            self._channel.settimeout(timeout_seconds)
            self._channel.sendall(encode_chf_runner_frame(request))
            return receive_chf_runner_frame(self._channel)
        except (OSError, ChfRunnerProtocolError, TypeError, ValueError) as error:
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
        error = ChfRunnerSessionRetired(message)
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
