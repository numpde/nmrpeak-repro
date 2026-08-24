"""Prove one-send Attempt outcomes preserve mutation uncertainty."""

from __future__ import annotations

import json
import unittest

from nmrpeak_provider.provider_https import (
    ProviderHttpResponse,
    ProviderOperation,
    ProviderRequestUnavailable,
    ProviderResponseRejected,
    ProviderTlsRejected,
    RequestDelivery,
    ResponseRejection,
)
from nmrpeak_provider.provider_outcomes import (
    AttemptMutationCommitPossible,
    AttemptMutationCommitted,
    AttemptMutationNotCommitted,
    interpret_execution_attempt_complete,
    interpret_execution_attempt_fail,
    interpret_execution_attempt_progress,
    interpret_execution_attempt_start,
)
from nmrpeak_provider.provider_problems import ProviderProblem, ProviderProblemRejected
from nmrpeak_provider.provider_requests import (
    _PreparedProviderRequest,
    prepare_execution_attempt_complete,
    prepare_execution_attempt_fail,
    prepare_execution_attempt_progress,
    prepare_execution_attempt_start,
)
from nmrpeak_provider.provider_success import (
    ExecutionAttemptCompleted,
    ExecutionAttemptFailed,
    ExecutionAttemptProgressed,
    ExecutionAttemptStarted,
    ProviderSuccessRejected,
)


_ATTEMPT_REF = "execution_attempt:sha256:" + "a" * 64


class ProviderOutcomeTests(unittest.TestCase):
    def test_problem_consequences_remain_operation_specific(self) -> None:
        cases = (
            (
                prepare_execution_attempt_start(
                    job_ref="job:test",
                    provider_attempt_key="stable-key",
                ),
                409,
                AttemptMutationCommitPossible,
            ),
            (
                prepare_execution_attempt_progress(
                    execution_attempt_ref=_ATTEMPT_REF,
                    phase="preparing",
                    condition_code=None,
                ),
                408,
                AttemptMutationNotCommitted,
            ),
            (
                prepare_execution_attempt_complete(
                    execution_attempt_ref=_ATTEMPT_REF,
                    result_schema_id="nmr.analysis_result.test.v1",
                    canonical_result=b"result",
                ),
                500,
                AttemptMutationCommitPossible,
            ),
            (
                prepare_execution_attempt_fail(
                    execution_attempt_ref=_ATTEMPT_REF,
                    failure_code="input_rejected",
                    failure_message="The input is not supported.",
                ),
                400,
                AttemptMutationNotCommitted,
            ),
        )
        for prepared, status, expected_type in cases:
            with self.subTest(operation=prepared.operation, status=status):
                outcome = _interpret(prepared, _problem_response(status))
                self.assertIs(type(outcome), expected_type)
                self.assertIs(type(outcome.evidence), ProviderProblem)
                self.assertEqual(outcome.evidence.status, status)

    def test_transport_evidence_does_not_overclaim_commit_state(self) -> None:
        prepared = prepare_execution_attempt_start(
            job_ref="job:test",
            provider_attempt_key="stable-key",
        )
        cases = (
            (ProviderTlsRejected(), AttemptMutationNotCommitted),
            (
                ProviderRequestUnavailable(RequestDelivery.NOT_SENT),
                AttemptMutationNotCommitted,
            ),
            (
                ProviderRequestUnavailable(RequestDelivery.POSSIBLE),
                AttemptMutationCommitPossible,
            ),
            (
                ProviderResponseRejected(ResponseRejection.INVALID_TOPOLOGY, 200),
                AttemptMutationCommitPossible,
            ),
        )
        for evidence, expected_type in cases:
            with self.subTest(evidence=evidence):
                self.assertIs(type(_interpret(prepared, evidence)), expected_type)

    def test_malformed_success_requires_reconciliation(self) -> None:
        prepared = prepare_execution_attempt_complete(
            execution_attempt_ref=_ATTEMPT_REF,
            result_schema_id="nmr.analysis_result.test.v1",
            canonical_result=b"result",
        )
        outcome = interpret_execution_attempt_complete(
            prepared,
            _json_response(200, {"schema_id": "wrong"}),
        )
        self.assertIs(type(outcome), AttemptMutationCommitPossible)
        self.assertIs(type(outcome.evidence), ProviderSuccessRejected)

    def test_malformed_problem_cannot_prove_the_apparent_rejection(self) -> None:
        prepared = prepare_execution_attempt_fail(
            execution_attempt_ref=_ATTEMPT_REF,
            failure_code="input_rejected",
            failure_message="The input is not supported.",
        )
        response = _json_response(
            408,
            {},
            content_type="application/problem+json",
            request_id="header-request",
        )
        outcome = interpret_execution_attempt_fail(prepared, response)
        self.assertIs(type(outcome), AttemptMutationCommitPossible)
        self.assertIs(type(outcome.evidence), ProviderProblemRejected)

    def test_command_bound_success_confirms_the_exact_completion(self) -> None:
        prepared = prepare_execution_attempt_complete(
            execution_attempt_ref=_ATTEMPT_REF,
            result_schema_id="nmr.analysis_result.test.v1",
            canonical_result=b"result",
        )
        response = _json_response(
            200,
            {
                "schema_id": "nmr.provider.execution_attempt_complete_response.v1",
                "execution_attempt_ref": _ATTEMPT_REF,
                "analysis_result_ref": "analysis_result:sha256:" + "b" * 64,
                "result_schema_id": "nmr.analysis_result.test.v1",
                "result_fingerprint": (
                    "sha256:"
                    "f6a214f7a5fcda0c2cee9660b7fc29f5649e3c68aad48e20e950137c98913a68"
                ),
                "result_byte_length": 6,
                "committed_at": "2026-08-24T12:00:00Z",
                "replayed": False,
            },
        )
        outcome = interpret_execution_attempt_complete(prepared, response)
        self.assertIs(type(outcome), AttemptMutationCommitted)
        self.assertIs(type(outcome.receipt), ExecutionAttemptCompleted)

    def test_each_other_success_interpreter_confirms_its_bound_receipt(self) -> None:
        cases = (
            (
                prepare_execution_attempt_start(
                    job_ref="job:test",
                    provider_attempt_key="stable-key",
                ),
                {
                    "schema_id": "nmr.provider.execution_attempt_start_response.v1",
                    "execution_attempt_ref": _ATTEMPT_REF,
                    "job_ref": "job:test",
                    "analysis_kind_ref": "mol_from_1h_peaks",
                    "provider_ref": "provider:test",
                    "state": "in_progress",
                    "started_at": "2026-08-24T12:00:00Z",
                    "replayed": False,
                },
                ExecutionAttemptStarted,
            ),
            (
                prepare_execution_attempt_progress(
                    execution_attempt_ref=_ATTEMPT_REF,
                    phase="preparing",
                    condition_code=None,
                ),
                {
                    "schema_id": "nmr.provider.execution_attempt_progress_response.v1",
                    "execution_attempt_ref": _ATTEMPT_REF,
                    "phase": "preparing",
                    "condition_code": None,
                    "updated_at": "2026-08-24T12:00:00Z",
                },
                ExecutionAttemptProgressed,
            ),
            (
                prepare_execution_attempt_fail(
                    execution_attempt_ref=_ATTEMPT_REF,
                    failure_code="input_rejected",
                    failure_message="The input is not supported.",
                ),
                {
                    "schema_id": "nmr.provider.execution_attempt_fail_response.v1",
                    "execution_attempt_ref": _ATTEMPT_REF,
                    "failure_code": "input_rejected",
                    "failure_message": "The input is not supported.",
                    "committed_at": "2026-08-24T12:00:00Z",
                    "replayed": False,
                },
                ExecutionAttemptFailed,
            ),
        )
        for prepared, document, receipt_type in cases:
            with self.subTest(operation=prepared.operation):
                outcome = _interpret(prepared, _json_response(200, document))
                self.assertIs(type(outcome), AttemptMutationCommitted)
                self.assertIs(type(outcome.receipt), receipt_type)


def _interpret(prepared: _PreparedProviderRequest, outcome):
    if prepared.operation is ProviderOperation.EXECUTION_ATTEMPT_START:
        return interpret_execution_attempt_start(
            prepared,
            outcome,
            expected_provider_ref="provider:test",
            expected_analysis_kind_ref="mol_from_1h_peaks",
        )
    if prepared.operation is ProviderOperation.EXECUTION_ATTEMPT_PROGRESS:
        return interpret_execution_attempt_progress(prepared, outcome)
    if prepared.operation is ProviderOperation.EXECUTION_ATTEMPT_COMPLETE:
        return interpret_execution_attempt_complete(prepared, outcome)
    return interpret_execution_attempt_fail(prepared, outcome)


def _problem_response(status: int) -> ProviderHttpResponse:
    problem_type, title = {
        400: ("urn:nmr-api:problem:bad-request", "Bad request"),
        401: (
            "urn:nmr-api:problem:authentication-failed",
            "Request authentication failed",
        ),
        403: ("urn:nmr-api:problem:authorization-denied", "Authorization denied"),
        404: ("urn:nmr-api:problem:not-found", "Resource not found"),
        408: (
            "urn:nmr-api:problem:request-body-timeout",
            "Request body timeout",
        ),
        409: ("urn:nmr-api:problem:operation-conflict", "Operation conflict"),
        413: (
            "urn:nmr-api:problem:request-content-too-large",
            "Request content too large",
        ),
        414: ("urn:nmr-api:problem:uri-too-long", "URI too long"),
        431: (
            "urn:nmr-api:problem:request-header-fields-too-large",
            "Request header fields too large",
        ),
        500: ("urn:nmr-api:problem:internal-error", "Internal server error"),
        503: ("urn:nmr-api:problem:service-unavailable", "Service unavailable"),
    }[status]
    document = {
        "type": problem_type,
        "title": title,
        "status": status,
        "instance": "/provider/v1/problems/test",
        "request_id": "body-request",
    }
    if status in {400, 413, 414, 431}:
        document |= {
            "code": {
                400: "request_query_not_supported",
                413: "request_content_too_large",
                414: "request_path_too_large",
                431: "request_header_bytes_too_large",
            }[status],
            "detail": "Correct the request.",
        }
    return _json_response(
        status,
        document,
        content_type="application/problem+json",
        request_id="header-request",
    )


def _json_response(
    status: int,
    document: dict[str, object],
    *,
    content_type: str = "application/json",
    request_id: str | None = None,
) -> ProviderHttpResponse:
    return ProviderHttpResponse(
        status=status,
        topology="dev-local",
        content_type=content_type,
        request_id=request_id,
        body=json.dumps(document, separators=(",", ":")).encode("utf-8"),
    )


if __name__ == "__main__":
    unittest.main()
