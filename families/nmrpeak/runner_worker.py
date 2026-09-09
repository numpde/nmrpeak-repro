"""Serve one loaded NMRPeak runtime over its concrete private codec."""

from __future__ import annotations

import logging
import time

from typing import Generic, Protocol, TypeVar

from nmrpeak_provider.canonical_json import JsonValue
from nmrpeak_provider.runner_protocol import (
    GenerateFrame,
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
                    'Input rejected; attempt=%s reason=%r',
                    command.correlation.attempt_ref,
                    str(rejection),
                )
                connection.sendall(
                    codec.encode(
                        RejectedFrame(command.correlation, str(rejection))
                    )
                )
                continue
            pending = command
            connection.sendall(codec.encode(ValidatedFrame(command.correlation)))
            continue
        if type(command) is GenerateFrame:
            if pending is None or command.correlation != pending.correlation:
                raise RunnerProtocolError(
                    "Cannot generate NMRPeak candidates: no matching input is validated"
                )
            _LOG.info("Model generation began; attempt=%s", command.correlation.attempt_ref)
            started_at = time.monotonic()
            candidates = runtime.generate(pending.model_input)
            connection.sendall(
                codec.encode(ResultFrame(command.correlation, candidates))
            )
            _LOG.info(
                'Model result sent to provider; attempt=%s elapsed_seconds=%.3f',
                command.correlation.attempt_ref,
                time.monotonic() - started_at,
            )
            pending = None
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
