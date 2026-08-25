"""Serve one loaded CHF runtime over the released private frame protocol."""

from __future__ import annotations

from typing import Protocol

from nmrpeak_provider.canonical_json import JsonValue
from nmrpeak_provider.chf_binding import ChfRunnerInput
from nmrpeak_provider.chf_runner_protocol import (
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


class ChfRuntimeInputRejected(ValueError):
    """The loaded tokenizer deterministically rejects one complete input."""


class LoadedChfRuntime(Protocol):
    """The checkpoint-backed behavior required by the CHF protocol loop."""

    def validate(self, model_input: ChfRunnerInput) -> None: ...

    def generate(self, model_input: ChfRunnerInput) -> JsonValue: ...


class ChfWorkerConnection(Protocol):
    """The inherited owner-session stream used by the serving worker."""

    def recv_into(self, buffer: memoryview) -> int: ...

    def sendall(self, data: bytes) -> None: ...


def serve_loaded_chf_runtime(
    connection: ChfWorkerConnection,
    runtime: LoadedChfRuntime,
    ready: ReadyFrame,
) -> int:
    """Publish READY and serve one validated request at a time until RETIRE."""

    connection.sendall(encode_chf_runner_frame(ready))
    pending: ValidateFrame | None = None
    while True:
        command = receive_chf_runner_frame(connection)
        if type(command) is ValidateFrame:
            if pending is not None:
                raise ChfRunnerProtocolError(
                    "Cannot validate CHF input: another request is already validated"
                )
            try:
                runtime.validate(command.model_input)
            except ChfRuntimeInputRejected:
                connection.sendall(
                    encode_chf_runner_frame(RejectedFrame(command.correlation))
                )
                continue
            pending = command
            connection.sendall(
                encode_chf_runner_frame(ValidatedFrame(command.correlation))
            )
            continue
        if type(command) is GenerateFrame:
            if pending is None or command.correlation != pending.correlation:
                raise ChfRunnerProtocolError(
                    "Cannot generate CHF candidates: no matching input is validated"
                )
            candidates = runtime.generate(pending.model_input)
            connection.sendall(
                encode_chf_runner_frame(ResultFrame(command.correlation, candidates))
            )
            pending = None
            continue
        if type(command) is RetireFrame:
            if pending is not None or command.boot_generation != ready.boot_generation:
                raise ChfRunnerProtocolError(
                    "Cannot retire CHF worker: boot is wrong or a request is pending"
                )
            return 0
        raise ChfRunnerProtocolError(
            "Cannot serve CHF worker: provider sent a response-only frame"
        )
