"""Prove the HF worker binds its verified runtime to HF READY facts."""

from __future__ import annotations

from models.nmrpeak_hf_v1.runner import worker
from nmrpeak_provider.hf_runner_protocol import (
    HF_RUNNER_CODEC,
    HF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.product_result import (
    HF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
)
from nmrpeak_provider.runner_protocol import ReadyFrame
import unittest
from unittest.mock import patch


BOOT = "boot:" + "1" * 32
CHECKPOINT_REF = "sha256:" + "2" * 64
IMAGE_INPUT_ID = "sha256:" + "3" * 64


class HfWorkerTests(unittest.TestCase):
    def test_worker_loads_the_verified_descriptor_before_publishing_ready(self) -> None:
        checkpoint = object()
        runtime = object()
        connection = object()
        expected_ready = ReadyFrame(
            boot_generation=BOOT,
            runner_ref=HF_RESULT_IDENTITY.runner_ref,
            runner_contract_id=HF_RUNNER_CONTRACT_ID,
            release_sha256=CHECKPOINT_REF,
            source_closure_sha256=NMRPEAK_SOURCE_CLOSURE_REF,
            image_input_id=IMAGE_INPUT_ID,
            target="cpu-x86_64",
            device="cpu",
            decode_policy_id=HF_RESULT_IDENTITY.decode_policy.decode_policy_id,
        )

        with (
            patch.object(
                worker,
                "open_verified_checkpoint",
                return_value=CheckpointContext(checkpoint),
            ) as open_checkpoint,
            patch.object(
                worker,
                "load_nmrpeak_hf_runtime",
                return_value=runtime,
            ) as load_runtime,
            patch.object(
                worker,
                "serve_loaded_nmrpeak_runtime",
                return_value=0,
            ) as serve,
        ):
            result = worker.serve_hf_worker(
                connection,
                checkpoint_ref=CHECKPOINT_REF,
                image_input_id=IMAGE_INPUT_ID,
                boot_generation=BOOT,
            )

        self.assertEqual(0, result)
        open_checkpoint.assert_called_once_with(CHECKPOINT_REF)
        load_runtime.assert_called_once_with(checkpoint)
        serve.assert_called_once_with(
            connection,
            runtime,
            expected_ready,
            HF_RUNNER_CODEC,
        )


class CheckpointContext:
    def __init__(self, checkpoint: object) -> None:
        self.checkpoint = checkpoint

    def __enter__(self) -> object:
        return self.checkpoint

    def __exit__(self, *_error: object) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
