"""Load the HF component and serve its released private runner protocol."""

from __future__ import annotations

import argparse
import secrets
import socket
import sys

from families.nmrpeak.checkpoint_file import open_verified_checkpoint
from families.nmrpeak.runner_worker import (
    WorkerConnection,
    serve_loaded_nmrpeak_runtime,
)
from models.nmrpeak_hf_v1.runner.runtime import load_nmrpeak_hf_runtime
from nmrpeak_provider.hf_runner_protocol import (
    HF_RUNNER_CODEC,
    HF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.product_decode import HF_DECODE_POLICY
from nmrpeak_provider.product_result import (
    HF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
)
from nmrpeak_provider.runner_protocol import ReadyFrame


def serve_hf_worker(
    connection: WorkerConnection,
    *,
    checkpoint_ref: str,
    image_input_id: str,
    boot_generation: str,
) -> int:
    """Load the fixed verified component and serve its inherited owner session."""

    with open_verified_checkpoint(checkpoint_ref) as checkpoint:
        runtime = load_nmrpeak_hf_runtime(checkpoint)
    ready = ReadyFrame(
        boot_generation=boot_generation,
        runner_ref=HF_RESULT_IDENTITY.runner_ref,
        runner_contract_id=HF_RUNNER_CONTRACT_ID,
        release_sha256=checkpoint_ref,
        source_closure_sha256=NMRPEAK_SOURCE_CLOSURE_REF,
        image_input_id=image_input_id,
        target="cpu-x86_64",
        device="cpu",
        decode_policy_id=HF_DECODE_POLICY.decode_policy_id,
    )
    return serve_loaded_nmrpeak_runtime(
        connection,
        runtime,
        ready,
        HF_RUNNER_CODEC,
    )


def main(arguments: list[str]) -> int:
    """Own one inherited session descriptor and one fixed HF model boot."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--session-fd", required=True, type=int)
    parser.add_argument("--checkpoint-ref", required=True)
    parser.add_argument("--image-input-id", required=True)
    options = parser.parse_args(arguments)
    with socket.socket(fileno=options.session_fd) as connection:
        return serve_hf_worker(
            connection,
            checkpoint_ref=options.checkpoint_ref,
            image_input_id=options.image_input_id,
            boot_generation="boot:" + secrets.token_hex(16),
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
