"""Validate operation-specific NMR API problem responses without retry policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re

from .provider_https import (
    ProviderHttpResponse,
    ProviderOperation,
    provider_operation_admits_status,
)
from .provider_response_json import decode_provider_response_object


_VISIBLE_ASCII = re.compile(r"[\x21-\x7e]{1,128}")
_EDGE_SPACE = re.compile(r"[ \u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
_FORBIDDEN_DIAGNOSTIC = re.compile(
    r"[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u200b-\u200f"
    r"\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]"
)
_STATUS_IDENTITY = {
    400: ("urn:nmr-api:problem:bad-request", "Bad request"),
    401: (
        "urn:nmr-api:problem:authentication-failed",
        "Request authentication failed",
    ),
    403: ("urn:nmr-api:problem:authorization-denied", "Authorization denied"),
    404: ("urn:nmr-api:problem:not-found", "Resource not found"),
    408: ("urn:nmr-api:problem:request-body-timeout", "Request body timeout"),
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
}
_READ_BAD_REQUEST_CODES = frozenset(
    {"provider_request_invalid", "request_content_not_supported"}
)
_MUTATION_BAD_REQUEST_CODES = frozenset(
    {"provider_request_invalid", "request_query_not_supported"}
)
_ATTEMPT_READ_BAD_REQUEST_CODES = _READ_BAD_REQUEST_CODES | {
    "request_query_not_supported"
}
_DIAGNOSTIC_CODES = {
    413: frozenset({"request_content_too_large"}),
    414: frozenset({"request_path_too_large", "request_query_too_large"}),
    431: frozenset(
        {"request_header_bytes_too_large", "request_header_count_too_large"}
    ),
}
_MUTATIONS = frozenset(
    {
        ProviderOperation.EXECUTION_ATTEMPT_COMPLETE,
        ProviderOperation.EXECUTION_ATTEMPT_FAIL,
        ProviderOperation.EXECUTION_ATTEMPT_START,
        ProviderOperation.EXECUTION_ATTEMPT_PROGRESS,
        ProviderOperation.PROVIDER_HELLO,
    }
)


class ProblemRejection(Enum):
    """Closed reasons a problem document cannot enter provider policy."""

    NOT_A_PROBLEM_RESPONSE = "not_a_problem_response"
    INVALID_JSON = "invalid_json"
    INVALID_FIELDS = "invalid_fields"
    INVALID_IDENTITY = "invalid_identity"
    INVALID_REQUEST_ID = "invalid_request_id"
    INVALID_DIAGNOSTIC = "invalid_diagnostic"


@dataclass(frozen=True, slots=True)
class ProviderProblem:
    """One exact API problem, preserving header and body request identities."""

    status: int
    problem_type: str
    title: str
    instance: str = field(repr=False)
    transport_request_id: str
    body_request_id: str
    code: str | None
    detail: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderProblemRejected:
    """A received body failed the pinned operation-specific problem contract."""

    reason: ProblemRejection
    status: int
    cause: BaseException | None = field(default=None, compare=False, repr=False)


def parse_provider_problem(
    operation: ProviderOperation,
    response: ProviderHttpResponse,
) -> ProviderProblem | ProviderProblemRejected:
    """Validate a received problem without assigning retry or commit meaning."""

    if type(operation) is not ProviderOperation:
        raise TypeError("Provider problem parsing requires an admitted operation")
    if type(response) is not ProviderHttpResponse:
        raise TypeError("Provider problem parsing requires an admitted HTTP response")
    if response.status == 200 or response.content_type != "application/problem+json":
        return ProviderProblemRejected(
            ProblemRejection.NOT_A_PROBLEM_RESPONSE,
            response.status,
        )
    if not provider_operation_admits_status(operation, response.status):
        return ProviderProblemRejected(
            ProblemRejection.NOT_A_PROBLEM_RESPONSE,
            response.status,
        )
    try:
        document = decode_provider_response_object(response.body)
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError) as error:
        return ProviderProblemRejected(
            ProblemRejection.INVALID_JSON,
            response.status,
            error,
        )
    diagnostic_codes = _diagnostic_codes(operation, response.status)
    expected_fields = {"type", "title", "status", "instance", "request_id"}
    if diagnostic_codes is not None:
        expected_fields |= {"code", "detail"}
    if set(document) != expected_fields:
        return ProviderProblemRejected(ProblemRejection.INVALID_FIELDS, response.status)
    expected_identity = _STATUS_IDENTITY.get(response.status)
    if expected_identity is None or (
        document["type"], document["title"], document["status"]
    ) != (*expected_identity, response.status):
        return ProviderProblemRejected(ProblemRejection.INVALID_IDENTITY, response.status)
    instance = document["instance"]
    body_request_id = document["request_id"]
    if type(instance) is not str or not 1 <= len(instance) <= 404:
        return ProviderProblemRejected(ProblemRejection.INVALID_FIELDS, response.status)
    if (
        type(body_request_id) is not str
        or _VISIBLE_ASCII.fullmatch(body_request_id) is None
        or response.request_id is None
    ):
        return ProviderProblemRejected(ProblemRejection.INVALID_REQUEST_ID, response.status)
    code = document.get("code")
    detail = document.get("detail")
    if diagnostic_codes is not None and (
        type(code) is not str
        or code not in diagnostic_codes
        or type(detail) is not str
        or not _is_safe_diagnostic(detail)
    ):
        return ProviderProblemRejected(ProblemRejection.INVALID_DIAGNOSTIC, response.status)
    return ProviderProblem(
        status=response.status,
        problem_type=expected_identity[0],
        title=expected_identity[1],
        instance=instance,
        transport_request_id=response.request_id,
        body_request_id=body_request_id,
        code=code,
        detail=detail,
    )


def _diagnostic_codes(
    operation: ProviderOperation,
    status: int,
) -> frozenset[str] | None:
    if status == 400:
        if operation is ProviderOperation.EXECUTION_ATTEMPT_READ:
            return _ATTEMPT_READ_BAD_REQUEST_CODES
        return (
            _MUTATION_BAD_REQUEST_CODES
            if operation in _MUTATIONS
            else _READ_BAD_REQUEST_CODES
        )
    return _DIAGNOSTIC_CODES.get(status)


def _is_safe_diagnostic(value: str) -> bool:
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if not value or len(value) > 1_024 or len(encoded) > 1_024:
        return False
    if _EDGE_SPACE.fullmatch(value[0]) or _EDGE_SPACE.fullmatch(value[-1]):
        return False
    return _FORBIDDEN_DIAGNOSTIC.search(value) is None
