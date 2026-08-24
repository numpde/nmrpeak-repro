"""Prove typed provider facts render exact replayable requests."""

from __future__ import annotations

from base64 import b64decode
import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nmrpeak_provider.provider_https import ProviderOperation
from nmrpeak_provider.provider_requests import (
    HelloOffering,
    prepare_execution_attempt_complete,
    prepare_execution_attempt_fail,
    prepare_execution_attempt_progress,
    prepare_execution_attempt_read,
    prepare_execution_attempt_start,
    prepare_execution_attempts_list,
    prepare_job_input_read,
    prepare_jobs_list,
    prepare_provider_hello,
    sign_prepared_provider_request,
)


ATTEMPT_REF = "execution_attempt:sha256:" + "1" * 64
PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


class ProviderRequestTests(unittest.TestCase):
    def test_read_operations_render_fixed_paths_and_query_order(self) -> None:
        requests = (
            prepare_execution_attempts_list(limit=50, cursor="AAAA"),
            prepare_jobs_list(
                analysis_kind_ref="mol_from_1h_peaks",
                has_provider_execution_attempt=False,
                limit=17,
                cursor="AAAA",
            ),
            prepare_job_input_read(
                job_ref="job:test",
                analysis_kind_ref="mol_from_1h_peaks",
            ),
            prepare_execution_attempt_read(ATTEMPT_REF),
        )
        self.assertEqual(
            [(request.operation, request.method, request.path, request.query) for request in requests],
            [
                (
                    ProviderOperation.EXECUTION_ATTEMPTS_LIST,
                    "GET",
                    "/provider/v1/execution-attempts",
                    "state=in_progress&limit=50&cursor=AAAA",
                ),
                (
                    ProviderOperation.JOBS_LIST,
                    "GET",
                    "/provider/v1/jobs",
                    (
                        "analysis_kind_ref=mol_from_1h_peaks&"
                        "has_provider_execution_attempt=false&limit=17&cursor=AAAA"
                    ),
                ),
                (
                    ProviderOperation.JOB_INPUT_READ,
                    "GET",
                    "/provider/v1/jobs/job:test/input",
                    "analysis_kind_ref=mol_from_1h_peaks",
                ),
                (
                    ProviderOperation.EXECUTION_ATTEMPT_READ,
                    "GET",
                    f"/provider/v1/execution-attempts/{ATTEMPT_REF}",
                    "",
                ),
            ],
        )
        self.assertTrue(all(request.body is None for request in requests))

    def test_start_and_progress_are_exact_canonical_commands(self) -> None:
        started = prepare_execution_attempt_start(
            job_ref="job:test",
            provider_attempt_key="nmrpeak-provider.v1:" + "2" * 64,
        )
        progressed = prepare_execution_attempt_progress(
            execution_attempt_ref=ATTEMPT_REF,
            phase="preparing",
            condition_code=None,
        )
        self.assertEqual(
            started.body,
            (
                b'{"job_ref":"job:test","provider_attempt_key":"'
                + b"nmrpeak-provider.v1:"
                + b"2" * 64
                + b'","schema_id":"nmr.provider.execution_attempt_start_request.v1"}'
            ),
        )
        self.assertEqual(
            progressed.body,
            (
                b'{"condition_code":null,"phase":"preparing","schema_id":"'
                b'nmr.provider.execution_attempt_progress_request.v1"}'
            ),
        )
        self.assertEqual(progressed.method, "PUT")

    def test_complete_base64_encodes_the_exact_result(self) -> None:
        prepared = prepare_execution_attempt_complete(
            execution_attempt_ref=ATTEMPT_REF,
            result_schema_id="nmrpeak.structure_candidates.result.v1",
            canonical_result=b'{"answer":true}',
        )
        document = json.loads(prepared.body)
        self.assertEqual(
            b64decode(document["canonical_result_base64"], validate=True),
            b'{"answer":true}',
        )
        self.assertEqual(
            document["schema_id"],
            "nmr.provider.execution_attempt_complete_request.v1",
        )

    def test_fail_preserves_only_the_reviewed_public_diagnostic(self) -> None:
        prepared = prepare_execution_attempt_fail(
            execution_attempt_ref=ATTEMPT_REF,
            failure_code="provider_input_rejected",
            failure_message="The submitted spectrum is outside the supported contract.",
        )
        self.assertEqual(
            json.loads(prepared.body),
            {
                "schema_id": "nmr.provider.execution_attempt_fail_request.v1",
                "execution_attempt_ref": ATTEMPT_REF,
                "failure_code": "provider_input_rejected",
                "failure_message": (
                    "The submitted spectrum is outside the supported contract."
                ),
            },
        )

    def test_hello_is_one_complete_canonical_snapshot(self) -> None:
        prepared = prepare_provider_hello(
            display_name="NMRPeak",
            description="Bounded structure candidates from structured NMR input.",
            analysis_offerings=(
                HelloOffering(
                    "mol_from_1h_peaks",
                    "Requires a molecular formula and structured 1H peaks.",
                ),
                HelloOffering(
                    "mol_from_1h_13c_formula",
                    "Requires a molecular formula and structured 1H and 13C peaks.",
                ),
            ),
        )
        document = json.loads(prepared.body)
        self.assertEqual(document["display_name"], "NMRPeak")
        self.assertEqual(
            [item["analysis_kind_ref"] for item in document["analysis_offerings"]],
            ["mol_from_1h_peaks", "mol_from_1h_13c_formula"],
        )
        self.assertEqual(prepared.body, _canonical_json(document))

    def test_resigning_preserves_business_bytes_and_refreshes_nonce(self) -> None:
        prepared = prepare_execution_attempt_start(
            job_ref="job:test",
            provider_attempt_key="attempt:test",
        )
        first = sign_prepared_provider_request(
            prepared,
            private_key=PRIVATE_KEY,
            credential_ref="credential:provider:test",
            authority="api.example.test",
            created=1_700_000_000,
            nonce=bytes(range(16)),
        )
        second = sign_prepared_provider_request(
            prepared,
            private_key=PRIVATE_KEY,
            credential_ref="credential:provider:test",
            authority="api.example.test",
            created=1_700_000_001,
            nonce=bytes(range(16, 32)),
        )
        self.assertEqual(first.body, second.body)
        self.assertEqual(first.raw_target, second.raw_target)
        self.assertNotEqual(first.headers["Signature"], second.headers["Signature"])

    def test_reference_query_and_page_bounds_fail_before_signing(self) -> None:
        invalid_calls = (
            lambda: prepare_execution_attempt_read("execution_attempt:bad"),
            lambda: prepare_job_input_read(
                job_ref="job:bad/path",
                analysis_kind_ref="mol_from_1h_peaks",
            ),
            lambda: prepare_jobs_list(analysis_kind_ref="Uppercase"),
            lambda: prepare_jobs_list(
                analysis_kind_ref="mol_from_1h_peaks",
                limit=True,
            ),
            lambda: prepare_execution_attempts_list(cursor="AB"),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises((TypeError, ValueError)):
                    call()

    def test_analysis_text_and_cursor_schema_edges_are_exact(self) -> None:
        longest_analysis_kind = "a" * 128
        self.assertIn(
            longest_analysis_kind,
            prepare_jobs_list(analysis_kind_ref=longest_analysis_kind).query,
        )
        with self.assertRaises(ValueError):
            prepare_jobs_list(analysis_kind_ref="a" * 129)
        for cursor in ("AA", "AAA", "AAAA"):
            with self.subTest(cursor=cursor):
                self.assertTrue(
                    prepare_execution_attempts_list(cursor=cursor).query.endswith(
                        f"cursor={cursor}"
                    )
                )
        for cursor in ("A", "AB", "AAB", "===="):
            with self.subTest(cursor=cursor):
                with self.assertRaises(ValueError):
                    prepare_execution_attempts_list(cursor=cursor)

    def test_hello_text_accepts_its_limit_and_rejects_non_scalar_text(self) -> None:
        prepared = prepare_provider_hello(
            display_name="n" * 128,
            description="d" * 1_024,
            analysis_offerings=(),
        )
        self.assertEqual(len(json.loads(prepared.body)["display_name"]), 128)
        with self.assertRaises(ValueError):
            prepare_provider_hello(
                display_name="bad\ud800",
                description="safe",
                analysis_offerings=(),
            )

    def test_terminal_command_bounds_are_closed(self) -> None:
        with self.assertRaises(TypeError):
            prepare_execution_attempt_complete(
                execution_attempt_ref=ATTEMPT_REF,
                result_schema_id="result.v1",
                canonical_result="not bytes",
            )
        with self.assertRaises(ValueError):
            prepare_execution_attempt_complete(
                execution_attempt_ref=ATTEMPT_REF,
                result_schema_id="result.v1",
                canonical_result=b"x" * 786_433,
            )
        with self.assertRaises(ValueError):
            prepare_execution_attempt_fail(
                execution_attempt_ref=ATTEMPT_REF,
                failure_code="contains-hyphen",
                failure_message="safe",
            )
        with self.assertRaises(ValueError):
            prepare_execution_attempt_fail(
                execution_attempt_ref=ATTEMPT_REF,
                failure_code="safe_code",
                failure_message="contains\0nul",
            )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
