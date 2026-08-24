"""Prove API problems are admitted only by their operation contract."""

from __future__ import annotations

import json
import unittest

from nmrpeak_provider.provider_https import ProviderHttpResponse, ProviderOperation
from nmrpeak_provider.provider_problems import (
    ProblemRejection,
    ProviderProblem,
    ProviderProblemRejected,
    parse_provider_problem,
)


class ProviderProblemTests(unittest.TestCase):
    def test_noncanonical_problem_json_preserves_independent_request_ids(self) -> None:
        response = _problem_response(
            400,
            {
                "detail": "Correct the signed request.",
                "code": "provider_request_invalid",
                "request_id": "body-request",
                "instance": "/provider/v1/problems/example",
                "status": 400,
                "title": "Bad request",
                "type": "urn:nmr-api:problem:bad-request",
            },
            header_request_id="header-request",
            pretty=True,
        )
        outcome = parse_provider_problem(ProviderOperation.JOBS_LIST, response)
        self.assertIs(type(outcome), ProviderProblem)
        self.assertEqual(outcome.transport_request_id, "header-request")
        self.assertEqual(outcome.body_request_id, "body-request")
        self.assertEqual(outcome.code, "provider_request_invalid")

    def test_fixed_problem_identities_are_exact(self) -> None:
        identities = {
            401: (
                "urn:nmr-api:problem:authentication-failed",
                "Request authentication failed",
            ),
            403: ("urn:nmr-api:problem:authorization-denied", "Authorization denied"),
            408: (
                "urn:nmr-api:problem:request-body-timeout",
                "Request body timeout",
            ),
            500: ("urn:nmr-api:problem:internal-error", "Internal server error"),
            503: (
                "urn:nmr-api:problem:service-unavailable",
                "Service unavailable",
            ),
        }
        for status, (problem_type, title) in identities.items():
            with self.subTest(status=status):
                outcome = parse_provider_problem(
                    ProviderOperation.JOBS_LIST,
                    _problem_response(
                        status,
                        _basic_document(status, problem_type, title),
                    ),
                )
                self.assertIs(type(outcome), ProviderProblem)

    def test_bad_request_codes_remain_operation_specific(self) -> None:
        cases = (
            (ProviderOperation.JOBS_LIST, "request_content_not_supported", True),
            (ProviderOperation.JOBS_LIST, "request_query_not_supported", False),
            (
                ProviderOperation.EXECUTION_ATTEMPT_READ,
                "request_query_not_supported",
                True,
            ),
            (
                ProviderOperation.EXECUTION_ATTEMPT_START,
                "request_query_not_supported",
                True,
            ),
            (
                ProviderOperation.EXECUTION_ATTEMPT_START,
                "request_content_not_supported",
                False,
            ),
        )
        for operation, code, accepted in cases:
            with self.subTest(operation=operation, code=code):
                document = _basic_document(
                    400,
                    "urn:nmr-api:problem:bad-request",
                    "Bad request",
                ) | {"code": code, "detail": "Correct the request."}
                outcome = parse_provider_problem(
                    operation,
                    _problem_response(400, document),
                )
                self.assertEqual(type(outcome) is ProviderProblem, accepted)

    def test_diagnostic_statuses_require_exact_codes_and_safe_detail(self) -> None:
        cases = (
            (413, "request_content_too_large"),
            (414, "request_path_too_large"),
            (414, "request_query_too_large"),
            (431, "request_header_bytes_too_large"),
            (431, "request_header_count_too_large"),
        )
        for status, code in cases:
            with self.subTest(status=status, code=code):
                problem_type, title = {
                    413: (
                        "urn:nmr-api:problem:request-content-too-large",
                        "Request content too large",
                    ),
                    414: ("urn:nmr-api:problem:uri-too-long", "URI too long"),
                    431: (
                        "urn:nmr-api:problem:request-header-fields-too-large",
                        "Request header fields too large",
                    ),
                }[status]
                document = _basic_document(status, problem_type, title) | {
                    "code": code,
                    "detail": "Reduce the request.",
                }
                outcome = parse_provider_problem(
                    ProviderOperation.EXECUTION_ATTEMPT_START,
                    _problem_response(status, document),
                )
                self.assertIs(type(outcome), ProviderProblem)

        invalid_details = (" leading", "trailing ", "line\nfeed", "line\u2028break")
        for detail in invalid_details:
            with self.subTest(detail=repr(detail)):
                document = _basic_document(
                    413,
                    "urn:nmr-api:problem:request-content-too-large",
                    "Request content too large",
                ) | {"code": "request_content_too_large", "detail": detail}
                self.assertEqual(
                    parse_provider_problem(
                        ProviderOperation.EXECUTION_ATTEMPT_START,
                        _problem_response(413, document),
                    ),
                    ProviderProblemRejected(ProblemRejection.INVALID_DIAGNOSTIC, 413),
                )

    def test_closed_shape_duplicate_json_and_identity_drift_are_rejected(self) -> None:
        document = _basic_document(
            401,
            "urn:nmr-api:problem:authentication-failed",
            "Request authentication failed",
        )
        extra = document | {"extra": True}
        wrong_title = document | {"title": "No"}
        cases = (
            (_problem_response(401, extra), ProblemRejection.INVALID_FIELDS),
            (_problem_response(401, wrong_title), ProblemRejection.INVALID_IDENTITY),
            (
                _raw_problem_response(
                    401,
                    b'{"type":"x","type":"y"}',
                ),
                ProblemRejection.INVALID_JSON,
            ),
        )
        for response, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    parse_provider_problem(ProviderOperation.JOBS_LIST, response),
                    ProviderProblemRejected(reason, 401),
                )

    def test_status_must_be_declared_by_the_operation(self) -> None:
        response = _problem_response(
            409,
            _basic_document(
                409,
                "urn:nmr-api:problem:operation-conflict",
                "Operation conflict",
            ),
        )
        self.assertEqual(
            parse_provider_problem(ProviderOperation.JOBS_LIST, response),
            ProviderProblemRejected(ProblemRejection.NOT_A_PROBLEM_RESPONSE, 409),
        )

    def test_parser_owned_json_and_diagnostic_boundaries_are_closed(self) -> None:
        deeply_nested = b"[" * 2_000 + b"]" * 2_000
        self.assertEqual(
            parse_provider_problem(
                ProviderOperation.JOBS_LIST,
                _raw_problem_response(400, deeply_nested),
            ),
            ProviderProblemRejected(ProblemRejection.INVALID_JSON, 400),
        )

        bool_status = _basic_document(
            400,
            "urn:nmr-api:problem:bad-request",
            "Bad request",
        ) | {
            "status": True,
            "code": "provider_request_invalid",
            "detail": "Correct the request.",
        }
        self.assertEqual(
            parse_provider_problem(
                ProviderOperation.JOBS_LIST,
                _problem_response(400, bool_status),
            ),
            ProviderProblemRejected(ProblemRejection.INVALID_IDENTITY, 400),
        )

        for detail, accepted in (
            ("x" * 1_024, True),
            ("x" * 1_025, False),
            ("é" * 512, True),
            ("é" * 513, False),
            ("bad\ud800", False),
        ):
            with self.subTest(length=len(detail), accepted=accepted):
                document = _basic_document(
                    400,
                    "urn:nmr-api:problem:bad-request",
                    "Bad request",
                ) | {
                    "code": "provider_request_invalid",
                    "detail": detail,
                }
                outcome = parse_provider_problem(
                    ProviderOperation.JOBS_LIST,
                    _problem_response(400, document),
                )
                self.assertEqual(type(outcome) is ProviderProblem, accepted)


def _basic_document(status: int, problem_type: str, title: str) -> dict[str, object]:
    return {
        "type": problem_type,
        "title": title,
        "status": status,
        "instance": "/provider/v1/problems/example",
        "request_id": "body-request",
    }


def _problem_response(
    status: int,
    document: dict[str, object],
    *,
    header_request_id: str = "header-request",
    pretty: bool = False,
) -> ProviderHttpResponse:
    separators = None if pretty else (",", ":")
    return _raw_problem_response(
        status,
        json.dumps(document, separators=separators).encode("utf-8"),
        header_request_id=header_request_id,
    )


def _raw_problem_response(
    status: int,
    body: bytes,
    *,
    header_request_id: str = "header-request",
) -> ProviderHttpResponse:
    return ProviderHttpResponse(
        status=status,
        topology="dev-local",
        content_type="application/problem+json",
        request_id=header_request_id,
        body=body,
    )


if __name__ == "__main__":
    unittest.main()
