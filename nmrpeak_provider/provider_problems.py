"""Validate operation-specific NMR API problem responses without retry policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ._nmr_api_failures import FailureInterpretation, interpret_problem

from .provider_https import ProviderHttpResponse, ProviderOperation


class ProblemRejection(Enum):
    """Closed reasons a problem document cannot enter provider policy."""

    NOT_A_PROBLEM_RESPONSE = "not_a_problem_response"
    INVALID_CURRENT_CONTRACT = "invalid_current_contract"


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
    upload_ref: str | None = None
    recovery_mode: str | None = None
    recovery_description: str | None = field(default=None, repr=False)
    current_send_effect: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderProblemRejected:
    """A received body failed the pinned operation-specific problem contract."""

    reason: ProblemRejection
    status: int
    diagnostic: FailureInterpretation | None = field(default=None, repr=False)


def parse_provider_problem(
    operation: ProviderOperation,
    response: ProviderHttpResponse,
) -> ProviderProblem | ProviderProblemRejected:
    """Validate a received problem without assigning retry or commit meaning."""

    if type(operation) is not ProviderOperation:
        raise TypeError("Provider problem parsing requires an admitted operation")
    if type(response) is not ProviderHttpResponse:
        raise TypeError("Provider problem parsing requires an admitted HTTP response")
    current = interpret_problem(
        operation=operation.value, status=response.status,
        content_type=response.content_type, header_request_id=response.request_id,
        body=response.body,
    )
    if not current.verified:
        return ProviderProblemRejected(
            (ProblemRejection.INVALID_CURRENT_CONTRACT if current.supported
             else ProblemRejection.NOT_A_PROBLEM_RESPONSE),
            response.status, diagnostic=current,
        )
    return ProviderProblem(
        status=current.status, problem_type=current.problem_type,
        title=current.title, instance=current.instance,
        transport_request_id=current.header_request_id,
        body_request_id=current.body_request_id, code=current.code,
        detail=current.detail, upload_ref=current.upload_ref,
        recovery_mode=current.recovery_mode,
        recovery_description=current.recovery_description,
        current_send_effect=current.current_send_effect,
    )
