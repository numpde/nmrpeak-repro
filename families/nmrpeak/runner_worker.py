"""Serve one loaded NMRPeak runtime over its concrete private codec."""

from __future__ import annotations

import logging
import time

from typing import Generic, Protocol, TypeVar

from nmrpeak_provider.diagnostic_text import exception_diagnostic
from nmrpeak_provider.canonical_json import JsonValue
from nmrpeak_provider.runner_protocol import (
    GenerateFrame,
    FailedFrame,
    AttemptCorrelation,
    ReadyFrame,
    RejectedFrame,
    ResultFrame,
    RetireFrame,
    RunnerFrameCodec,
    RunnerModelInput,
    RunnerProtocolError,
    ValidateFrame,
    ValidatedFrame,
)

from .runner_runtime import NmrpeakRuntimeInputRejected


_LOG = logging.getLogger("nmrpeak_runner.worker")


ModelInput = TypeVar("ModelInput", bound=RunnerModelInput)


class LoadedNmrpeakRuntime(Protocol, Generic[ModelInput]):
    """The checkpoint-backed behavior required by one runner protocol loop."""

    def validate(self, model_input: ModelInput) -> None: ...

    def generate(self, model_input: ModelInput) -> JsonValue: ...


class WorkerConnection(Protocol):
    """The inherited owner-session stream used by the serving worker."""

    def recv_into(self, buffer: memoryview) -> int: ...

    def sendall(self, data: bytes) -> None: ...


def serve_loaded_nmrpeak_runtime(
    connection: WorkerConnection,
    runtime: LoadedNmrpeakRuntime[ModelInput],
    ready: ReadyFrame,
    codec: RunnerFrameCodec[ModelInput],
) -> int:
    """Publish READY and serve one validated request at a time until RETIRE."""

    connection.sendall(codec.encode(ready))
    _LOG.info(
        'Runner ready; runner=%s boot=%s checkpoint=%s',
        ready.runner_ref,
        ready.boot_generation,
        ready.release_sha256,
    )
    pending: ValidateFrame[ModelInput] | None = None
    while True:
        command = codec.receive(connection)
        if type(command) is ValidateFrame:
            if pending is not None:
                raise RunnerProtocolError(
                    "Cannot validate NMRPeak input: another request is already validated"
                )
            _LOG.info("Validating input; attempt=%s", command.correlation.attempt_ref)
            try:
                runtime.validate(command.model_input)
            except NmrpeakRuntimeInputRejected as rejection:
                _LOG.info(
                    'Runner validation rejected; attempt=%s reason=%s',
                    command.correlation.attempt_ref,
                    rejection.reason.value,
                )
                response = codec.encode(RejectedFrame(
                    command.correlation, rejection.reason,
                    str(rejection), rejection.token_count,
                ))
            except BaseException as error:
                _report_failure(connection, codec, command.correlation, "validate", error)
                raise
            else:
                pending = command
                response = codec.encode(ValidatedFrame(command.correlation))
            if pending is None:
                # Leave the caught rejection's traceback and decoded input
                # behind before its reply permits another Attempt to reuse us.
                command = None
            connection.sendall(response)
            continue
        if type(command) is GenerateFrame:
            if pending is None or command.correlation != pending.correlation:
                raise RunnerProtocolError(
                    "Cannot generate NMRPeak candidates: no matching input is validated"
                )
            _LOG.info("Model generation began; attempt=%s", command.correlation.attempt_ref)
            started_at = time.monotonic()
            try:
                candidates = runtime.generate(pending.model_input)
                response = codec.encode(ResultFrame(command.correlation, candidates))
            except BaseException as error:
                _report_failure(connection, codec, command.correlation, "generate", error)
                raise
            # The reply releases this request for warm reuse. Drop its input
            # before the owner can observe that completion; a partial write
            # cannot be followed by another trustworthy frame.
            pending = None
            connection.sendall(response)
            _LOG.info(
                'Model result sent to provider; attempt=%s elapsed_seconds=%.3f',
                command.correlation.attempt_ref,
                time.monotonic() - started_at,
            )
            continue
        if type(command) is RetireFrame:
            if pending is not None or command.boot_generation != ready.boot_generation:
                raise RunnerProtocolError(
                    "Cannot retire NMRPeak worker: boot is wrong or a request is pending"
                )
            _LOG.info("Runner retired; boot=%s", ready.boot_generation)
            return 0
        raise RunnerProtocolError(
            "Cannot serve NMRPeak worker: provider sent a response-only frame"
        )


def _report_failure(
    connection: WorkerConnection, codec: RunnerFrameCodec[ModelInput],
    correlation: AttemptCorrelation, phase: str, error: BaseException,
) -> None:
    try:
        connection.sendall(codec.encode(FailedFrame(correlation, phase, exception_diagnostic(error))))
    except BaseException as reporting_error:
        error.add_note("Worker failure report delivery is unconfirmed: " + exception_diagnostic(reporting_error))
    _LOG.error("Runner %s failed; attempt=%s; %s", phase, correlation.attempt_ref, exception_diagnostic(error))
