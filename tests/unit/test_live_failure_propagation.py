from __future__ import annotations

import json
from base64 import b64encode
from hashlib import sha256
from pathlib import Path
import stat
import tempfile
import unittest

from nmrpeak_provider.input_issue_message import render_direct_runner_rejection
from nmrpeak_provider.canonical_json import parse_canonical_json_bytes
from nmrpeak_provider.hf_runner_protocol import HF_RUNNER_CONTRACT_ID
from nmrpeak_provider.product import HF_OFFERING, NMRPEAK_PRODUCT
from nmrpeak_provider.product_input import ChfModelInput, HfModelInput, parse_job_input
from nmrpeak_provider.product_result import HF_RESULT_IDENTITY, ProviderResultFacts, canonical_result_bytes
from tests.live.failure_propagation import (
    LiveTestError,
    _all_attempts,
    _failure_evidence,
    _failure_summary,
    _input_document,
    _is_job_ref,
    _negative_attempt_outcome,
    _exclusive_state_lock,
    _public_problem_evidence,
    _reject_cancelled_without_attempt,
    _strict_json,
    _validate_state_shape,
    _write_state,
)
from tests.live.success_propagation import (
    _success_input_document,
    _validate_product_result,
    _validate_result,
)


class LiveFailurePropagationTests(unittest.TestCase):
    def test_unexpected_failure_summary_does_not_repeat_message(self) -> None:
        item = {
            "execution_attempt_ref": "execution_attempt:sha256:" + "a" * 64,
            "provider_failure": {"code": "runner_failed", "message": "sensitive detail"},
        }
        summary = _failure_summary(item)
        self.assertEqual(summary["failure_code"], "runner_failed")
        self.assertEqual(summary["message_byte_length"], 16)
        self.assertNotIn("sensitive detail", json.dumps(summary))

    def test_attempt_failure_message_limit_counts_unicode_scalars(self) -> None:
        class Api:
            def request(self, _method, _path, _query):
                return {
                    "schema_id": "nmr.job.execution_attempt.list.response.v1",
                    "job_ref": "job:test",
                    "next_cursor": None,
                    "attempts": [{
                        "execution_attempt_ref": "execution_attempt:sha256:" + "a" * 64,
                        "provider_ref": "provider:nmrpeak",
                        "provider_condition": None,
                        "provider_failure": {"code": "runner_failed", "message": "é" * 600},
                        "started_at": "2026-10-05T12:00:00Z",
                        "state": "failed",
                        "updated_at": "2026-10-05T12:00:01Z",
                    }],
                }

        self.assertEqual(len(_all_attempts(Api(), "job:test")), 1)

    def test_cancelled_job_without_attempt_is_reported_immediately(self) -> None:
        with self.assertRaisesRegex(LiveTestError, "cancelled before"):
            _reject_cancelled_without_attempt(
                {"state": "cancelled"}, [], "negative hf", "job:test"
            )
        _reject_cancelled_without_attempt(
            {"state": "cancelled"}, [{"state": "failed"}], "negative hf", "job:test"
        )

    def test_job_reference_rejects_path_delimiters(self) -> None:
        self.assertTrue(_is_job_ref("job:valid_ref-1.2"))
        self.assertFalse(_is_job_ref("job:valid/escape"))
        self.assertFalse(_is_job_ref("job:valid?query"))

    def test_negative_qualifier_reports_terminal_nonfailure_immediately(self) -> None:
        attempt = {
            "execution_attempt_ref": "execution_attempt:sha256:" + "a" * 64,
            "provider_ref": "provider:nmrpeak",
            "state": "succeeded",
        }
        with self.assertRaisesRegex(
            LiveTestError, r"negative hf Attempt execution_attempt:sha256:a+ ended in state succeeded"
        ):
            _negative_attempt_outcome(attempt, "provider:nmrpeak", "hf")

    def test_public_problem_evidence_is_closed_bounded_and_correlated(self) -> None:
        problem = {
            "instance": "/v1/jobs", "request_id": "request:test", "status": 503,
            "title": "Unavailable", "type": "urn:test", "code": "unavailable",
            "detail": "Try again using the retained request bytes.",
        }
        payload = json.dumps(problem).encode()
        expected = {key: problem[key] for key in ("type", "title", "code", "detail")}
        self.assertEqual(
            _public_problem_evidence(
                payload, content_type="application/problem+json", status=503,
                request_id="request:test",
            ),
            expected,
        )
        for change in (
            {"content_type": "application/json"},
            {"status": 500},
            {"request_id": "request:other"},
        ):
            arguments = {
                "content_type": "application/problem+json", "status": 503,
                "request_id": "request:test", **change,
            }
            self.assertEqual(_public_problem_evidence(payload, **arguments), {})
        problem["detail"] = "x" * 1025
        self.assertEqual(
            _public_problem_evidence(
                json.dumps(problem).encode(), content_type="application/problem+json",
                status=503, request_id="request:test",
            ),
            {},
        )
        problem["detail"] = "\ud800"
        self.assertEqual(
            _public_problem_evidence(
                json.dumps(problem).encode(), content_type="application/problem+json",
                status=503, request_id="request:test",
            ),
            {},
        )

    def test_strict_json_rejects_duplicate_members(self) -> None:
        with self.assertRaises(ValueError):
            _strict_json(b'{"message":"private","message":"public"}')

    def test_state_lock_rejects_a_concurrent_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            path = directory / "state.json"
            with _exclusive_state_lock(path):
                with self.assertRaisesRegex(LiveTestError, "already owned"):
                    with _exclusive_state_lock(path):
                        self.fail("second owner acquired the state lock")

    def test_generated_inputs_are_admitted_by_both_product_lanes(self) -> None:
        hf = _input_document("hf", 220)
        chf = _input_document("chf", 220)
        self.assertIsInstance(parse_job_input(hf.encode(), HF_OFFERING), HfModelInput)
        self.assertIsInstance(parse_job_input(chf.encode(), NMRPEAK_PRODUCT.offerings[1]), ChfModelInput)
        self.assertNotEqual(hf, chf)
        self.assertLess(len(hf.encode()), 65536)
        self.assertLess(len(chf.encode()), 65536)

        self.assertIsInstance(
            parse_job_input(_success_input_document("hf").encode(), HF_OFFERING),
            HfModelInput,
        )
        self.assertIsInstance(
            parse_job_input(
                _success_input_document("chf").encode(), NMRPEAK_PRODUCT.offerings[1]
            ),
            ChfModelInput,
        )

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

    def test_attempt_list_rejects_extra_nested_failure_details(self) -> None:
        class Api:
            def request(self, _method, _path, _query):
                return {
                    "schema_id": "nmr.job.execution_attempt.list.response.v1",
                    "job_ref": "job:test",
                    "next_cursor": None,
                    "attempts": [{
                        "execution_attempt_ref": "execution_attempt:sha256:" + "a" * 64,
                        "provider_ref": "provider:nmrpeak",
                        "provider_condition": None,
                        "provider_failure": {
                            "code": "input_rejected", "message": "safe", "raw_report": "private"
                        },
                        "started_at": "2026-10-05T12:00:00Z",
                        "state": "failed",
                        "updated_at": "2026-10-05T12:00:01Z",
                    }],
                }

        with self.assertRaisesRegex(LiveTestError, "provider failure"):
            _all_attempts(Api(), "job:test")

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

    def test_live_state_requires_exactly_one_job_per_lane(self) -> None:
        specification = _input_document("hf", 2)
        entry = {
            "lane": "hf",
            "analysis_kind_ref": "mol_from_1h_peaks",
            "description": "HF state",
            "operation_ref": "operation:hf-state",
            "create_body_text": json.dumps({
                "analysis_kind_ref": "mol_from_1h_peaks",
                "description": "HF state",
                "operation_ref": "operation:hf-state",
                "project_ref": "project:test",
                "specification": specification,
                "state": "closed",
            }, separators=(",", ":"), sort_keys=True),
        }
        state = {
            "schema_id": "nmrpeak.live_failure_propagation.state.v1",
            "api_origin": "https://api.example.test",
            "expected_topology": "web",
            "project_ref": "project:test",
            "provider_ref": "provider:test",
            "run_label": "test",
            "jobs": [entry],
        }
        with self.assertRaisesRegex(LiveTestError, "exactly two"):
            _validate_state_shape(state, state["schema_id"])

    def test_positive_result_reuses_exact_production_contract(self) -> None:
        artifact = {
            "checkpoint_sha256": "sha256:" + "a" * 64,
            "image_input_id": "sha256:" + "b" * 64,
        }
        facts = ProviderResultFacts(
            HF_RESULT_IDENTITY,
            HF_RUNNER_CONTRACT_ID,
            artifact["checkpoint_sha256"],
            artifact["image_input_id"],
        )
        raw = canonical_result_bytes(["CCO"], facts)
        document = parse_canonical_json_bytes(raw)
        _validate_product_result(document, raw, "hf", artifact)

        forged = json.loads(raw)
        forged["provenance"]["runner_contract_id"] = "nmrpeak.runner_session.chf.v1"
        forged_raw = json.dumps(
            forged, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        with self.assertRaisesRegex(LiveTestError, "exact lane and artifact"):
            _validate_product_result(forged, forged_raw, "hf", artifact)

    def test_positive_result_binds_one_attempt_one_result_and_exact_bytes(self) -> None:
        artifact = {
            "checkpoint_sha256": "sha256:" + "a" * 64,
            "image_input_id": "sha256:" + "b" * 64,
        }
        raw = canonical_result_bytes(
            ["CCO"],
            ProviderResultFacts(
                HF_RESULT_IDENTITY,
                HF_RUNNER_CONTRACT_ID,
                artifact["checkpoint_sha256"],
                artifact["image_input_id"],
            ),
        )
        attempt_ref = "execution_attempt:sha256:" + "c" * 64
        result_ref = "analysis_result:sha256:" + "d" * 64
        fingerprint = "sha256:" + sha256(raw).hexdigest()
        item = {
            "analysis_result_ref": result_ref,
            "execution_attempt_ref": attempt_ref,
            "created_at": "2026-10-05T12:00:00Z",
            "provider_ref": "provider:nmrpeak",
            "result_byte_length": len(raw),
            "result_fingerprint": fingerprint,
            "result_schema_id": "nmrpeak.structure_candidates.result.v1",
        }

        class Api:
            def request(self, _method, path, query=""):
                if path.endswith("/analysis-results"):
                    self.assert_query = query
                    return {
                        "schema_id": "nmr.job.analysis_result.list.response.v1",
                        "job_ref": "job:positive", "results": [item], "next_cursor": None,
                    }
                return {
                    **item,
                    "schema_id": "nmr.job.analysis_result.read.response.v1",
                    "job_ref": "job:positive",
                    "canonical_result_base64": b64encode(raw).decode(),
                }

        evidence = _validate_result(
            Api(),
            job_ref="job:positive",
            lane="hf",
            provider_ref="provider:nmrpeak",
            attempt={"execution_attempt_ref": attempt_ref},
            expected_artifacts={"hf": artifact},
        )
        self.assertEqual(evidence["candidate_count"], 1)

        class ExtraResultApi(Api):
            def request(self, method, path, query=""):
                response = super().request(method, path, query)
                if path.endswith("/analysis-results"):
                    response["results"] = [item, {**item, "analysis_result_ref": result_ref + "x"}]
                return response

        with self.assertRaisesRegex(LiveTestError, "exactly one bound"):
            _validate_result(
                ExtraResultApi(),
                job_ref="job:positive",
                lane="hf",
                provider_ref="provider:nmrpeak",
                attempt={"execution_attempt_ref": attempt_ref},
                expected_artifacts={"hf": artifact},
            )

        class MalformedResultRefApi(Api):
            def request(self, method, path, query=""):
                response = super().request(method, path, query)
                if path.endswith("/analysis-results"):
                    response["results"] = [
                        {**item, "analysis_result_ref": result_ref + "/escape"}
                    ]
                return response

        with self.assertRaisesRegex(LiveTestError, "invalid reference"):
            _validate_result(
                MalformedResultRefApi(),
                job_ref="job:positive",
                lane="hf",
                provider_ref="provider:nmrpeak",
                attempt={"execution_attempt_ref": attempt_ref},
                expected_artifacts={"hf": artifact},
            )


if __name__ == "__main__":
    unittest.main()
