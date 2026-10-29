"""Own one admitted NMRPeak runner boot and its single in-flight request."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import errno
from enum import StrEnum
import os
import secrets
import socket
import stat
from threading import Lock
import time
from typing import Generic, Protocol, TypeVar

from .diagnostic_text import diagnostic_text
from .canonical_json import JsonValue
from .runner_protocol import (
    AttemptCorrelation,
    RunnerProtocolError,
    RunnerChannelClosed,
    GenerateFrame,
    ReadyFrame,
    RejectedFrame,
    ResultFrame,
    FailedFrame,
    RunnerFrameCodec,
    RunnerModelInput,
    RunnerRejectionReason,
    ValidateFrame,
    ValidatedFrame,
    runner_rejection_diagnostic,
)
from .owner_session_endpoint import open_owner_session_directory
from .product_result import NMRPEAK_SOURCE_CLOSURE_REF, ProviderResultFacts
from . import native_policy
from .provider_config import RunnerDeadlines
from provider_core.async_runner_session import AsyncRunnerSession, await_original
from provider_core.task_boundary import contain_task_exit
from provider_core.runner import LOCK_NAME
from provider_core.runner_session import (
    RunnerSession as NativeControlSession,
    RunnerStartupStopped,
    RunnerStartupUnavailable,
)


ModelInput = TypeVar("ModelInput", bound=RunnerModelInput)


class _RunnerChannel(Protocol):
    def settimeout(self, value: float) -> None: ...
    def sendall(self, data: bytes) -> None: ...
    def recv_into(self, buffer: memoryview) -> int: ...
    def shutdown(self, how: int) -> None: ...
    def close(self) -> None: ...


class _DeadlineReceiver:
    """Keep fragmented reads inside the owning scientific phase's deadline."""

    def __init__(self, channel, deadline):
        self._channel, self._deadline = channel, deadline

    def recv_into(self, buffer):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Cannot receive scientific frame: phase deadline expired")
        self._channel.settimeout(remaining)
        return self._channel.recv_into(buffer)


class RunnerAdmissionError(RuntimeError):
    """A connected runner did not establish its provider-admitted boot."""

    def __init__(self, message, *, owner=None):
        self.owner = owner
        super().__init__(message)


class RunnerSessionRetired(RuntimeError):
    """The scientific boot is unusable; native retirement requires its own proof."""


class ScientificCallInterrupted(RunnerSessionRetired):
    """The lane withdrew its original scientific call before an outcome."""


class NativeObservationRequired(RunnerSessionRetired):
    """Science ended without an outcome; observe native exit before local stop."""


class NativeWorkerExited(RunnerSessionRetired):
    """Original native evidence, without granting permission to report an Attempt."""

    def __init__(self, native, cause):
        self.native, self.cause = native, cause
        super().__init__(
            f"The scientific worker exited before returning a verified outcome (status {native.original_exit.returncode})."
        )


class RunnerValidationDrift(RuntimeError):
    """Runner VALIDATE exposed tokenizer/checkpoint behavior requiring operator review."""

    def __init__(self, reason: RunnerRejectionReason) -> None:
        self.reason = reason
        super().__init__(f"NMRPeak runner validation drift: {reason.value}; {runner_rejection_diagnostic(reason)}")


@dataclass(frozen=True, slots=True)
class RunnerInputRejected:
    """The runner proved that a complete input exceeds its token limit."""

    message: str
    token_count: int
    reason: RunnerRejectionReason = RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED

    def __post_init__(self) -> None:
        if self.reason is not RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED or (
            self.message != runner_rejection_diagnostic(self.reason, self.token_count)
        ):
            raise RunnerProtocolError("Runner input rejection has invalid token evidence")


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
    ADMITTING = "admitting"
    IDLE = "idle"
    VALIDATING = "validating"
    VALIDATED = "validated"
    GENERATING = "generating"
    RETIRED = "retired"


async def open_runner_session(
    socket_path: str,
    facts: ProviderResultFacts,
    deadlines: RunnerDeadlines,
    codec: RunnerFrameCodec[ModelInput],
) -> RunnerSession[ModelInput]:
    """Acquire native control, then qualify its scientific READY under one budget."""
    owner = await prepare_runner_session(socket_path, facts, deadlines, codec)
    return await owner.connect()


async def prepare_runner_session(socket_path, facts, deadlines, codec):
    """Create the retained owner before its control connection can launch science."""
    if deadlines.retire_seconds < native_policy.RETIREMENT_SECONDS:
        raise ValueError(
            f"Native retirement requires at least {native_policy.RETIREMENT_SECONDS} seconds for TERM, KILL, scratch cleanup and observation"
        )
    started = time.monotonic()
    connect_deadline = started + deadlines.connect_seconds
    parent, socket_name = open_owner_session_directory(socket_path)
    try:
        try:
            endpoint = os.stat(socket_name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(endpoint.st_mode):
                raise RunnerAdmissionError("Cannot connect to runner: endpoint is not a Unix socket")
            if endpoint.st_uid != os.geteuid() or stat.S_IMODE(endpoint.st_mode) != 0o600:
                raise RunnerAdmissionError("Cannot connect to runner: endpoint is not owner-only")
        while True:
            remaining = _remaining_connect_seconds(connect_deadline)
            try:
                descriptor = os.open(LOCK_NAME, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            except FileNotFoundError:
                await asyncio.sleep(min(0.01, remaining))
                continue
            try:
                observed = os.fstat(descriptor)
                if (not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid()
                        or stat.S_IMODE(observed.st_mode) != 0o600):
                    raise RunnerAdmissionError("Native lifetime lock must be an owner-only regular file")
                identity = observed.st_dev, observed.st_ino
            finally:
                os.close(descriptor)
            break
    finally:
        os.close(parent)
    native = NativeControlSession(
        socket_path, role=facts.identity.runner_ref, expected_lock_identity=identity,
        startup_seconds=deadlines.connect_seconds,
        retirement_seconds=deadlines.retire_seconds,
    )
    return RunnerSession(None, facts, deadlines, codec, None, native=native, startup_started=started)


class RunnerSession(Generic[ModelInput]):
    """Serialize one provider owner session against one admitted runner boot."""

    def __init__(
        self,
        channel: _RunnerChannel | None,
        facts: ProviderResultFacts,
        deadlines: RunnerDeadlines,
        codec: RunnerFrameCodec[ModelInput],
        boot_generation: str | None,
        *,
        native: NativeControlSession,
        startup_started: float,
    ) -> None:
        self._channel = channel
        self._facts = facts
        self._deadlines = deadlines
        self._codec = codec
        self._boot_generation = boot_generation
        self._state = _SessionState.ADMITTING if boot_generation is None else _SessionState.IDLE
        self._pending: ValidatedRunnerRequest | None = None
        self._lock = Lock()
        self.native = native
        self._scientific_call = self._observed_scientific_call = None
        self.control = AsyncRunnerSession(native, setup_not_after=startup_started + deadlines.connect_seconds)
        self._science_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nmrpeak-science")
        self._science_shutdown = self._retirement_task = None
        self._science_closed = channel is None
        self._science_close_started = False
        self._native_closed = False
        self._startup_no_effect = None
        self._startup_started = startup_started

    async def connect(self):
        """Join the original native setup, then this lane's scientific READY."""
        if self._channel is not None:
            raise ValueError("Cannot reconnect a runner: retain its original admitted stream")
        try:
            await self.control.connect(wait_seconds=self._deadlines.connect_seconds)
            self._channel = self.native.science
            self._science_closed = False
            deadline = time.monotonic() + self._deadlines.ready_seconds
            original = self._submit_science(self._admit_ready, deadline)
            return await self.join_science(original)
        except BaseException as cause:
            await self._admission_failed(cause)

    def _admit_ready(self, deadline):
        if type(self._facts) is not ProviderResultFacts or type(self._deadlines) is not RunnerDeadlines:
            raise TypeError("Runner admission requires provider-owned result facts and deadlines")
        try:
            frame = self._receive_before(deadline)
        except BaseException as cause:
            if self._local_science_close(cause):
                raise ScientificCallInterrupted("Scientific READY was interrupted by local retirement") from cause
            raise
        if type(frame) is not ReadyFrame:
            raise RunnerProtocolError("Cannot admit runner: the first scientific frame is not READY")
        if not _ready_matches_provider_facts(frame, self._facts):
            raise RunnerProtocolError("Cannot admit runner: scientific READY facts differ from the deployment")
        if time.monotonic() > deadline:
            raise TimeoutError("Scientific readiness exceeded the original startup budget")
        with self._lock:
            if self._state is not _SessionState.ADMITTING:
                raise ScientificCallInterrupted("Cannot admit scientific READY after local retirement")
            self._boot_generation = frame.boot_generation
            self._state = _SessionState.IDLE
        return self

    def _no_effect_setup(self, cause):
        return (type(cause) in (RunnerStartupStopped, RunnerStartupUnavailable)
                and not self.native.control_connected
                and self.control.setup_failure is cause)

    async def _admission_failed(self, cause):
        error = RunnerAdmissionError(
            f"Cannot admit {self._codec.lane_name} runner boot from its native/scientific READY exchange",
            owner=self,
        )
        failures = []
        if self._no_effect_setup(cause):
            self._startup_no_effect = cause
        if self._unexpected_close(cause):
            try:
                cause = await self.observe_failure(cause)
            except BaseException as observation:
                failures.append(observation)
        try:
            await self.retire()
        except BaseException as cleanup:
            failures.append(cleanup)
        if failures:
            raise error from BaseExceptionGroup("Startup and independent retirement failures", [cause, *failures])
        raise error from cause

    @property
    def retirement_confirmed(self):
        shutdown = self._science_shutdown
        return (self._science_closed and self._native_closed
                and shutdown is not None and shutdown.done() and not shutdown.cancelled()
                and shutdown.exception() is None
                and (self._scientific_call is None or self._scientific_call.done())
                and (self._startup_no_effect is not None or (
                    self.native.retirement is not None and self.native.retirement.scratch_released)))

    def _submit_science(self, operation, *args, **kwargs):
        with self._lock:
            if self._state is _SessionState.RETIRED or self._science_shutdown is not None:
                raise RunnerSessionRetired("Cannot submit scientific work after lane retirement")
            if (self._scientific_call is not None
                    and self._observed_scientific_call is not self._scientific_call):
                raise RuntimeError("Cannot overlap scientific calls; join the original operation")
            original = self._science_executor.submit(operation, *args, **kwargs)
            self._scientific_call, self._observed_scientific_call = original, None
            return original

    @property
    def result_facts(self) -> ProviderResultFacts:
        """Return the provider authority retained independently of READY echoes."""

        return self._facts

    @property
    def quiescent(self) -> bool:
        """Confirm a matching scientific response left no pending request or call."""
        with self._lock:
            return (self._state is _SessionState.IDLE and self._pending is None
                    and not self._science_closed
                    and (self._scientific_call is None
                         or self._observed_scientific_call is self._scientific_call))

    @property
    def retired(self) -> bool:
        """Report whether this admitted boot can no longer accept work."""

        with self._lock:
            return self._state is _SessionState.RETIRED

    def validate(self, *, execution_attempt_ref, provider_attempt_key, model_input):
        return self._submit_science(self._validate, execution_attempt_ref=execution_attempt_ref,
                                    provider_attempt_key=provider_attempt_key, model_input=model_input,
                                    deadline=time.monotonic() + self._deadlines.validate_seconds)

    def _validate(
        self,
        *,
        execution_attempt_ref: str,
        provider_attempt_key: str,
        model_input: ModelInput,
        deadline: float,
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
            if self._state is _SessionState.RETIRED:
                raise ScientificCallInterrupted("Validation was withdrawn before scientific dispatch")
            wrong_state = self._state is not _SessionState.IDLE
            if not wrong_state:
                self._state = _SessionState.VALIDATING
        if wrong_state:
            self._retire_with_error(
                f"Cannot validate {self._codec.lane_name} runner input: the session is not idle"
            )

        response = self._exchange(
            frame,
            deadline,
            f"validate {self._codec.lane_name} runner input",
        )
        if type(response) is FailedFrame and response.correlation == correlation and response.phase == "validate":
            self._retire_with_error(
                "Runner reported validation failure: " + diagnostic_text(response.diagnostic)
                + ". No verified validation outcome was returned.",
                observed_failure=True,
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
                self._state = (
                    _SessionState.IDLE
                    if response.reason is RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED
                    else _SessionState.RETIRED
                )
                self._pending = None
            if response.reason is not RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED:
                raise RunnerValidationDrift(response.reason)
            return RunnerInputRejected(
                response.diagnostic, response.token_count, response.reason
            )
        self._retire_with_error(
            f"Cannot validate {self._codec.lane_name} runner input: "
            "response type or correlation is wrong"
        )

    def generate(self, request):
        return self._submit_science(self._generate, request,
                                    deadline=time.monotonic() + self._deadlines.generate_seconds)

    def _generate(self, request: ValidatedRunnerRequest, *, deadline: float) -> GeneratedRunnerCandidates:
        """Authorize model execution for the exact validated request."""

        with self._lock:
            if self._state is _SessionState.RETIRED:
                raise ScientificCallInterrupted("Generation was withdrawn before scientific dispatch")
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
            deadline,
            f"generate {self._codec.lane_name} candidates",
        )
        if type(response) is FailedFrame and response.correlation == request._correlation and response.phase == "generate":
            self._retire_with_error(
                "Runner reported generation failure: " + diagnostic_text(response.diagnostic)
                + ". No verified generation outcome was returned.",
                observed_failure=True,
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

    @staticmethod
    def _unexpected_close(cause):
        if isinstance(cause, RunnerChannelClosed):
            return True
        # The scientific decoder preserves an original socket failure as its
        # cause. A timeout or malformed frame still supplies no exit evidence.
        if isinstance(cause, RunnerProtocolError):
            cause = cause.__cause__
        return (isinstance(cause, RunnerChannelClosed)
                or (isinstance(cause, OSError)
                    and cause.errno in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE)))

    def _local_science_close(self, cause):
        return self.retired and self._science_close_started and (
            self._unexpected_close(cause)
            or isinstance(cause, OSError) and cause.errno in (errno.EBADF, errno.ENOTCONN))

    async def join_science(self, original):
        """Join this stream's original call and retain whether its outcome was seen."""
        if original is not self._scientific_call:
            raise ValueError("Cannot join scientific work from another lane or operation")
        try:
            result = await await_original(original, deadline=None)
        except BaseException as cause:
            if isinstance(cause, NativeObservationRequired):
                observed = await self.observe_failure(cause)
                if observed is not cause:
                    self._observed_scientific_call = original
                    raise observed from cause
            if original.done() and not original.cancelled() and original.exception() is cause:
                self._observed_scientific_call = original
            raise
        self._observed_scientific_call = original
        return result

    async def observe_failure(self, cause):
        """Observe the original native reader before causing a local interruption."""
        try:
            await self.control.observe_retirement(
                wait_seconds=min(native_policy.OBSERVATION_SECONDS, self._deadlines.retire_seconds / 5),
            )
        except TimeoutError as observation:
            if self.control.retirement_failure is observation:
                raise
        if self.native.original_exit is not None:
            return NativeWorkerExited(self.native, cause)
        return cause

    def _close_science(self):
        if self._science_closed:
            return
        self._science_close_started = True
        error = _close_channel(self._channel)
        if error is not None:
            raise RunnerSessionRetired("Scientific channel closure is unconfirmed") from error
        self._science_closed = True

    def cancel(self):
        """Mark interruption before requesting native stop and closing science."""
        with self._lock:
            self._state = _SessionState.RETIRED
            self._pending = None
        self.native.mark_interruption()
        failures = []
        if self.native.retirement is None:
            try:
                self.control.request_stop()
            except BaseException as cause:
                failures.append(cause)
        try:
            self._close_science()
        except BaseException as cause:
            failures.append(cause)
        if failures:
            raise RunnerSessionRetired("Native stop or scientific channel closure remains unconfirmed") from BaseExceptionGroup(
                "Independent stop/close failures", failures,
            )

    async def retire(self):
        """Join original scientific/native work and the physical SCI worker exit."""
        if self.retirement_confirmed:
            return
        if self._retirement_task is None or self._retirement_task.done():
            self._retirement_task = asyncio.create_task(contain_task_exit(self._retire))
        await asyncio.shield(self._retirement_task)

    async def _retire(self):
        failures = []
        try:
            self.cancel()
        except BaseException as cause:
            failures.append(cause)
        deadline = self.control.retirement_deadline
        original = self._scientific_call
        if original is not None:
            try:
                await await_original(original, deadline=deadline)
            except BaseException as cause:
                if not original.done() or original.cancelled():
                    failures.append(cause)
                else:
                    original_failure = original.exception()
                    if cause is not original_failure:
                        failures.append(cause)
                    if (original_failure is not None and original is not self._observed_scientific_call
                            and not isinstance(original_failure, ScientificCallInterrupted)):
                        failures.append(original_failure)
        if original is None or original.done():
            if self._science_shutdown is None:
                self._science_executor.shutdown(wait=False)
                self._science_shutdown = asyncio.create_task(
                    contain_task_exit(asyncio.to_thread, self._science_executor.shutdown, wait=True),
                )
            try:
                async with asyncio.timeout(max(0, deadline - time.monotonic())):
                    await asyncio.shield(self._science_shutdown)
            except BaseException as cause:
                failures.append(cause)
        if self._startup_no_effect is None:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Cannot join original native retirement: its aggregate allowance expired")
                await self.control.join_retirement(wait_seconds=remaining)
            except BaseException as cause:
                if self._no_effect_setup(cause):
                    self._startup_no_effect = cause
                else:
                    failures.append(cause)
        reader = self.control.reader
        native_complete = self._startup_no_effect is not None or (
            reader is not None and reader.done() and not reader.cancelled()
            and reader.exception() is None
            and self.native.retirement is not None and self.native.retirement.scratch_released)
        if native_complete:
            try:
                self.native.close()
                self._native_closed = True
            except BaseException as cause:
                failures.append(cause)
        if self.retirement_confirmed:
            self._scientific_call = self._observed_scientific_call = None
        if failures:
            raise RunnerSessionRetired(
                "Original lane retirement failed; retain its operations and exclusion until closure is confirmed"
            ) from BaseExceptionGroup("Original scientific/native retirement failures", failures)

    def _exchange(
        self,
        request: ValidateFrame | GenerateFrame,
        deadline: float,
        operation: str,
    ) -> object:
        try:
            encoded = self._codec.encode(request)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Cannot send scientific request: phase deadline expired")
            self._channel.settimeout(remaining)
            self._channel.sendall(encoded)
            return self._receive_before(deadline)
        except (OSError, RunnerProtocolError, TypeError, ValueError) as error:
            self._retire_with_error(
                f"Cannot {operation}: the runner exchange failed",
                cause=error,
            )

    def _receive_before(self, deadline):
        frame = self._codec.receive(_DeadlineReceiver(self._channel, deadline))
        if time.monotonic() > deadline:
            raise TimeoutError("Cannot accept scientific frame: phase deadline expired")
        return frame

    def _retire_with_error(self, message, *, cause=None, observed_failure=False):
        interrupted = self._local_science_close(cause)
        observing = not self.retired and (observed_failure or self._unexpected_close(cause))
        with self._lock:
            self._state = _SessionState.RETIRED
            self._pending = None
        if interrupted:
            raise ScientificCallInterrupted(message) from cause
        error = NativeObservationRequired(message) if observing else RunnerSessionRetired(message)
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
