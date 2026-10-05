from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest

from nmrpeak_provider.input_issue_message import render_direct_runner_rejection
from nmrpeak_provider.product import HF_OFFERING, NMRPEAK_PRODUCT
from nmrpeak_provider.product_input import ChfModelInput, HfModelInput, parse_job_input
from tests.live.failure_propagation import LiveTestError, _failure_evidence, _input_document, _write_state


class LiveFailurePropagationTests(unittest.TestCase):
    def test_generated_inputs_are_admitted_by_both_product_lanes(self) -> None:
        hf = _input_document("hf", 220)
        chf = _input_document("chf", 220)
        self.assertIsInstance(parse_job_input(hf.encode(), HF_OFFERING), HfModelInput)
        self.assertIsInstance(parse_job_input(chf.encode(), NMRPEAK_PRODUCT.offerings[1]), ChfModelInput)
        self.assertNotEqual(hf, chf)
        self.assertLess(len(hf.encode()), 65536)
        self.assertLess(len(chf.encode()), 65536)

    def test_failure_evidence_requires_exact_provider_and_canonical_message(self) -> None:
        message = render_direct_runner_rejection(640)
        item = {"execution_attempt_ref": "execution_attempt:sha256:" + "a" * 64,
                "provider_ref": "provider:nmrpeak", "state": "failed",
                "provider_failure": {"code": "input_rejected", "message": message}}
        self.assertEqual(_failure_evidence(item, "provider:nmrpeak")["token_count"], 640)
        with self.assertRaises(LiveTestError):
            _failure_evidence(item, "provider:other")
        item["provider_failure"] = {"code": "input_rejected", "message": message + " altered"}
        with self.assertRaises(LiveTestError):
            _failure_evidence(item, "provider:nmrpeak")

    def test_state_write_is_owner_only_and_atomic_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            path = directory / "state.json"
            _write_state(path, {"step": 1})
            _write_state(path, {"step": 2})
            self.assertEqual(json.loads(path.read_text()), {"step": 2})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(directory.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
