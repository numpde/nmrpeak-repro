"""Serve one loaded CHF runtime over the released private frame protocol."""

from __future__ import annotations

import argparse
import secrets
import socket
import sys
from typing import Protocol

from nmrpeak_provider.canonical_json import JsonValue
from nmrpeak_provider.chf_binding import ChfRunnerInput
from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CONTRACT_ID,
    CHF_RUNNER_CODEC,
)
from nmrpeak_provider.runner_protocol import (
    RunnerProtocolError,
    GenerateFrame,
    ReadyFrame,
    RejectedFrame,
    ResultFrame,
    RetireFrame,
    ValidateFrame,
    ValidatedFrame,
)
from nmrpeak_provider.product_decode import CHF_DECODE_POLICY
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
)
from models.nmrpeak_chf_v1.runner.checkpoint_file import (
    open_verified_chf_checkpoint,
)
from families.nmrpeak.runner_runtime import NmrpeakRuntimeInputRejected
from models.nmrpeak_chf_v1.runner.runtime import load_nmrpeak_chf_runtime


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

    connection.sendall(CHF_RUNNER_CODEC.encode(ready))
    pending: ValidateFrame | None = None
    while True:
        command = CHF_RUNNER_CODEC.receive(connection)
        if type(command) is ValidateFrame:
            if pending is not None:
                raise RunnerProtocolError(
                    "Cannot validate CHF input: another request is already validated"
                )
            try:
                runtime.validate(command.model_input)
            except NmrpeakRuntimeInputRejected:
                connection.sendall(
                    CHF_RUNNER_CODEC.encode(RejectedFrame(command.correlation))
                )
                continue
            pending = command
            connection.sendall(
                CHF_RUNNER_CODEC.encode(ValidatedFrame(command.correlation))
            )
            continue
        if type(command) is GenerateFrame:
            if pending is None or command.correlation != pending.correlation:
                raise RunnerProtocolError(
                    "Cannot generate CHF candidates: no matching input is validated"
                )
            candidates = runtime.generate(pending.model_input)
            connection.sendall(
                CHF_RUNNER_CODEC.encode(ResultFrame(command.correlation, candidates))
            )
            pending = None
            continue
        if type(command) is RetireFrame:
            if pending is not None or command.boot_generation != ready.boot_generation:
                raise RunnerProtocolError(
                    "Cannot retire CHF worker: boot is wrong or a request is pending"
                )
            return 0
        raise RunnerProtocolError(
            "Cannot serve CHF worker: provider sent a response-only frame"
        )


def serve_chf_worker(
    connection: ChfWorkerConnection,
    *,
    checkpoint_ref: str,
    image_input_id: str,
    boot_generation: str,
) -> int:
    """Load the fixed verified component and serve its inherited owner session."""

    with open_verified_chf_checkpoint(checkpoint_ref) as checkpoint:
        runtime = load_nmrpeak_chf_runtime(checkpoint)
    ready = ReadyFrame(
        boot_generation=boot_generation,
        runner_ref=CHF_RESULT_IDENTITY.runner_ref,
        runner_contract_id=CHF_RUNNER_CONTRACT_ID,
        release_sha256=checkpoint_ref,
        source_closure_sha256=NMRPEAK_SOURCE_CLOSURE_REF,
        image_input_id=image_input_id,
        target="cpu-x86_64",
        device="cpu",
        decode_policy_id=CHF_DECODE_POLICY.decode_policy_id,
    )
    return serve_loaded_chf_runtime(connection, runtime, ready)


def main(arguments: list[str]) -> int:
    """Own one inherited session descriptor and one fixed CHF model boot."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--session-fd", required=True, type=int)
    parser.add_argument("--checkpoint-ref", required=True)
    parser.add_argument("--image-input-id", required=True)
    options = parser.parse_args(arguments)
    with socket.socket(fileno=options.session_fd) as connection:
        return serve_chf_worker(
            connection,
            checkpoint_ref=options.checkpoint_ref,
            image_input_id=options.image_input_id,
            boot_generation="boot:" + secrets.token_hex(16),
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
