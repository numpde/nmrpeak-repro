"""Prove successful provider receipts bind to their originating operation."""

from __future__ import annotations

import json
from hashlib import sha256
import unittest

from nmrpeak_provider.provider_https import ProviderHttpResponse
from nmrpeak_provider.provider_requests import (
    prepare_execution_attempt_complete,
    prepare_execution_attempt_fail,
    prepare_execution_attempt_progress,
    prepare_execution_attempt_read,
    prepare_execution_attempt_start,
    prepare_provider_hello,
)
from nmrpeak_provider.provider_success import (
    AttemptState,
    ExecutionAttemptSnapshot,
    ExecutionAttemptStarted,
    ExecutionAttemptCompleted,
    ExecutionAttemptFailed,
    ExecutionAttemptProgressed,
    JobState,
    ProviderHelloAccepted,
    ProviderSuccessRejected,
    SuccessRejection,
    parse_execution_attempt_read_success,
    parse_execution_attempt_start_success,
    parse_execution_attempt_complete_success,
    parse_execution_attempt_fail_success,
    parse_execution_attempt_progress_success,
    parse_provider_hello_success,
)


ATTEMPT_REF = "execution_attempt:sha256:" + "1" * 64


class ProviderSuccessTests(unittest.TestCase):
    def test_hello_accepts_ordinary_json_and_binds_provider_identity(self) -> None:
        prepared = prepare_provider_hello(
            display_name="NMRPeak",
            description="Provider description.",
            analysis_offerings=(),
        )
        response = _success_response(
            {
                "accepted_at": "2026-08-24T12:00:00Z",
                "provider_ref": "provider:nmrpeak",
                "schema_id": "nmr.provider.hello_response.v1",
            },
            pretty=True,
        )
        self.assertEqual(
            parse_provider_hello_success(
                prepared,
                response,
                expected_provider_ref="provider:nmrpeak",
            ),
            ProviderHelloAccepted(
                provider_ref="provider:nmrpeak",
                accepted_at="2026-08-24T12:00:00Z",
            ),
        )

    def test_start_binds_job_provider_analysis_and_fresh_state(self) -> None:
        prepared = prepare_execution_attempt_start(
            job_ref="job:test",
            provider_attempt_key="attempt:test",
        )
        document = _start_document()
        outcome = parse_execution_attempt_start_success(
            prepared,
            _success_response(document),
            expected_provider_ref="provider:nmrpeak",
            expected_analysis_kind_ref="mol_from_1h_peaks",
        )
        self.assertEqual(
            outcome,
            ExecutionAttemptStarted(
                execution_attempt_ref=ATTEMPT_REF,
                job_ref="job:test",
                analysis_kind_ref="mol_from_1h_peaks",
                provider_ref="provider:nmrpeak",
                state=AttemptState.IN_PROGRESS,
                started_at="2026-08-24T12:00:00.123456Z",
                replayed=False,
            ),
        )

    def test_replayed_start_may_report_an_existing_terminal_state(self) -> None:
        prepared = prepare_execution_attempt_start(
            job_ref="job:test",
            provider_attempt_key="attempt:test",
        )
        document = _start_document() | {"state": "succeeded", "replayed": True}
        outcome = parse_execution_attempt_start_success(
            prepared,
            _success_response(document),
            expected_provider_ref="provider:nmrpeak",
            expected_analysis_kind_ref="mol_from_1h_peaks",
        )
        self.assertIs(type(outcome), ExecutionAttemptStarted)
        self.assertIs(outcome.state, AttemptState.SUCCEEDED)

    def test_start_rejects_each_drift_and_a_fresh_terminal_receipt(self) -> None:
        prepared = prepare_execution_attempt_start(
            job_ref="job:test",
            provider_attempt_key="attempt:test",
        )
        cases = (
            {"job_ref": "job:other"},
            {"provider_ref": "provider:other"},
            {"analysis_kind_ref": "other_kind"},
            {"state": "failed", "replayed": False},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                outcome = parse_execution_attempt_start_success(
                    prepared,
                    _success_response(_start_document() | changed),
                    expected_provider_ref="provider:nmrpeak",
                    expected_analysis_kind_ref="mol_from_1h_peaks",
                )
                self.assertEqual(
                    outcome,
                    ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT),
                )

    def test_point_read_binds_attempt_and_retained_job(self) -> None:
        prepared = prepare_execution_attempt_read(ATTEMPT_REF)
        response = _success_response(
            {
                "schema_id": "nmr.provider.execution_attempt_read_response.v1",
                "execution_attempt_ref": ATTEMPT_REF,
                "job_ref": "job:test",
                "state": "expired",
                "job_state": "cancelled",
            }
        )
        self.assertEqual(
            parse_execution_attempt_read_success(
                prepared,
                response,
                expected_job_ref="job:test",
            ),
            ExecutionAttemptSnapshot(
                ATTEMPT_REF,
                "job:test",
                AttemptState.EXPIRED,
                JobState.CANCELLED,
            ),
        )

    def test_closed_shape_schema_timestamp_and_operation_are_strict(self) -> None:
        hello = prepare_provider_hello(
            display_name="NMRPeak",
            description="Provider description.",
            analysis_offerings=(),
        )
        base = {
            "schema_id": "nmr.provider.hello_response.v1",
            "provider_ref": "provider:nmrpeak",
            "accepted_at": "2026-08-24T12:00:00Z",
        }
        cases = (
            (base | {"extra": True}, SuccessRejection.INVALID_SHAPE),
            (base | {"schema_id": "wrong"}, SuccessRejection.INVALID_FIELD),
            (base | {"accepted_at": "2026-02-30T12:00:00Z"}, SuccessRejection.INVALID_FIELD),
            (base | {"accepted_at": "2026-08-24T12:00:00.000000Z"}, SuccessRejection.INVALID_FIELD),
        )
        for document, reason in cases:
            with self.subTest(reason=reason, document=document):
                self.assertEqual(
                    parse_provider_hello_success(
                        hello,
                        _success_response(document),
                        expected_provider_ref="provider:nmrpeak",
                    ),
                    ProviderSuccessRejected(reason),
                )
        with self.assertRaises(TypeError):
            parse_provider_hello_success(
                prepare_execution_attempt_read(ATTEMPT_REF),
                _success_response(base),
                expected_provider_ref="provider:nmrpeak",
            )

    def test_progress_receipt_echoes_the_complete_snapshot(self) -> None:
        prepared = prepare_execution_attempt_progress(
            execution_attempt_ref=ATTEMPT_REF,
            phase="running",
            condition_code="runner_busy",
        )
        document = {
            "schema_id": "nmr.provider.execution_attempt_progress_response.v1",
            "execution_attempt_ref": ATTEMPT_REF,
            "phase": "running",
            "condition_code": "runner_busy",
            "updated_at": "2026-08-24T12:00:00Z",
        }
        self.assertEqual(
            parse_execution_attempt_progress_success(
                prepared,
                _success_response(document),
            ),
            ExecutionAttemptProgressed(
                ATTEMPT_REF,
                "running",
                "runner_busy",
                "2026-08-24T12:00:00Z",
            ),
        )
        self.assertEqual(
            parse_execution_attempt_progress_success(
                prepared,
                _success_response(document | {"condition_code": None}),
            ),
            ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT),
        )

    def test_complete_receipt_binds_exact_result_bytes_and_schema(self) -> None:
        result = b'{"candidates":[]}'
        prepared = prepare_execution_attempt_complete(
            execution_attempt_ref=ATTEMPT_REF,
            result_schema_id="nmrpeak.result.v1",
            canonical_result=result,
        )
        fingerprint = "sha256:" + sha256(result).hexdigest()
        document = {
            "schema_id": "nmr.provider.execution_attempt_complete_response.v1",
            "execution_attempt_ref": ATTEMPT_REF,
            "analysis_result_ref": "analysis_result:sha256:" + "2" * 64,
            "result_schema_id": "nmrpeak.result.v1",
            "result_fingerprint": fingerprint,
            "result_byte_length": len(result),
            "committed_at": "2026-08-24T12:00:00Z",
            "replayed": False,
        }
        self.assertEqual(
            parse_execution_attempt_complete_success(
                prepared,
                _success_response(document),
            ),
            ExecutionAttemptCompleted(
                ATTEMPT_REF,
                "analysis_result:sha256:" + "2" * 64,
                "2026-08-24T12:00:00Z",
                False,
            ),
        )
        for changed in (
            {"result_schema_id": "other"},
            {"result_fingerprint": "sha256:" + "0" * 64},
            {"result_byte_length": len(result) + 1},
        ):
            with self.subTest(changed=changed):
                self.assertEqual(
                    parse_execution_attempt_complete_success(
                        prepared,
                        _success_response(document | changed),
                    ),
                    ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT),
                )

    def test_fail_receipt_compares_but_does_not_retain_diagnostic_text(self) -> None:
        message = "The input is outside the supported contract."
        prepared = prepare_execution_attempt_fail(
            execution_attempt_ref=ATTEMPT_REF,
            failure_code="provider_input_rejected",
            failure_message=message,
        )
        document = {
            "schema_id": "nmr.provider.execution_attempt_fail_response.v1",
            "execution_attempt_ref": ATTEMPT_REF,
            "failure_code": "provider_input_rejected",
            "failure_message": message,
            "committed_at": "2026-08-24T12:00:00Z",
            "replayed": True,
        }
        outcome = parse_execution_attempt_fail_success(
            prepared,
            _success_response(document),
        )
        self.assertEqual(
            outcome,
            ExecutionAttemptFailed(
                ATTEMPT_REF,
                "2026-08-24T12:00:00Z",
                True,
            ),
        )
        self.assertNotIn(message, repr(outcome))
        self.assertEqual(
            parse_execution_attempt_fail_success(
                prepared,
                _success_response(document | {"failure_message": "changed"}),
            ),
            ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT),
        )

    def test_malformed_or_non_success_body_never_becomes_a_receipt(self) -> None:
        hello = prepare_provider_hello(
            display_name="NMRPeak",
            description="Provider description.",
            analysis_offerings=(),
        )
        malformed = ProviderHttpResponse(
            200,
            "dev-local",
            "application/json",
            None,
            b'{"schema_id":"x","schema_id":"y"}',
        )
        self.assertEqual(
            parse_provider_hello_success(
                hello,
                malformed,
                expected_provider_ref="provider:nmrpeak",
            ),
            ProviderSuccessRejected(SuccessRejection.INVALID_JSON),
        )


def _start_document() -> dict[str, object]:
    return {
        "schema_id": "nmr.provider.execution_attempt_start_response.v1",
        "execution_attempt_ref": ATTEMPT_REF,
        "job_ref": "job:test",
        "analysis_kind_ref": "mol_from_1h_peaks",
        "provider_ref": "provider:nmrpeak",
        "state": "in_progress",
        "started_at": "2026-08-24T12:00:00.123456Z",
        "replayed": False,
    }


def _success_response(
    document: dict[str, object],
    *,
    pretty: bool = False,
) -> ProviderHttpResponse:
    separators = None if pretty else (",", ":")
    return ProviderHttpResponse(
        status=200,
        topology="dev-local",
        content_type="application/json",
        request_id=None,
        body=json.dumps(document, separators=separators).encode("utf-8"),
    )


if __name__ == "__main__":
    unittest.main()
