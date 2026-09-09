"""Serve one loaded CHF runtime over the released private frame protocol."""

from __future__ import annotations

import argparse
import logging
import time
import secrets
import socket
import sys

from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CONTRACT_ID,
    CHF_RUNNER_CODEC,
)
from nmrpeak_provider.runner_protocol import (
    ReadyFrame,
)
from nmrpeak_provider.process_logging import configure_process_logging
from nmrpeak_provider.product_decode import CHF_DECODE_POLICY
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
)
from families.nmrpeak.checkpoint_file import open_verified_checkpoint
from families.nmrpeak.runner_worker import (
    WorkerConnection,
    serve_loaded_nmrpeak_runtime,
)
from models.nmrpeak_chf_v1.runner.runtime import load_nmrpeak_chf_runtime


_LOG = logging.getLogger("nmrpeak_runner.worker")


def serve_chf_worker(
    connection: WorkerConnection,
    *,
    checkpoint_ref: str,
    image_input_id: str,
    boot_generation: str,
) -> int:
    """Load the fixed verified component and serve its inherited owner session."""

    started_at = time.monotonic()
    _LOG.info(
        'Loading model; checkpoint=%s image=%s boot=%s',
        checkpoint_ref,
        image_input_id,
        boot_generation,
    )
    with open_verified_checkpoint(checkpoint_ref) as checkpoint:
        runtime = load_nmrpeak_chf_runtime(checkpoint)
    _LOG.info("Model loaded on CPU; elapsed_seconds=%.3f", time.monotonic() - started_at)
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
    return serve_loaded_nmrpeak_runtime(
        connection,
        runtime,
        ready,
        CHF_RUNNER_CODEC,
    )


def main(arguments: list[str]) -> int:
    """Own one inherited session descriptor and one fixed CHF model boot."""

    configure_process_logging()
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
