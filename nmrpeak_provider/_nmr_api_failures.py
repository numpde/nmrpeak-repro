"""Interpret the current provider 403 envelope without owning recovery actions.

Other statuses are explicitly unsupported in this first shared client slice.
A verified refusal describes this response; it never resolves an earlier mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re
from urllib.parse import quote

from ._nmr_api_failure_contract import OPERATIONS, PROFILE


@dataclass(frozen=True, slots=True)
class FailureInterpretation:
    supported: bool
    verified: bool
    rejection: str | None
    status: int
    problem_type: str | None = None
    title: str | None = None
    code: str | None = None
    detail: str | None = field(default=None, repr=False)
    body_request_id: str | None = None
    header_request_id: str | None = None
    instance: str | None = field(default=None, repr=False)


def _text(value: object, profile: dict) -> str | None:
    if type(value) is not str or not profile["minLength"] <= len(value) <= profile["maxLength"]:
        return None
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        return None
    if len(encoded) > profile.get("x-nmr-max-utf8-bytes", 4 * profile["maxLength"]):
        return None
    if "pattern" in profile and re.search(profile["pattern"], value) is None:
        return None
    if "not" in profile and re.search(profile["not"]["pattern"], value) is not None:
        return None
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON numeric constant")


def interpret_problem(*, operation: str, status: int, content_type: str | None,
                      header_request_id: str | None, body: bytes) -> FailureInterpretation:
    """Retain bounded evidence while reserving authority for correlated problems.

The transport supplies one header value; duplicates must be passed as None.
The decoded body's fields are never evidence of success or an earlier outcome.
"""
    result = FailureInterpretation(
        supported=type(status) is int and status == 403 and type(operation) is str and operation in OPERATIONS,
        verified=False, rejection="unsupported", status=status,
        header_request_id=_text(header_request_id, PROFILE["request_id"]),
    )
    if not result.supported:
        return result
    if content_type != "application/problem+json":
        return replace(result, rejection="content_type")
    # Source authority: nmr_api/security/problems.py:PROBLEM_RESPONSE_MAX_BODY_BYTES.
    # The API problem boundary's maximum is 4096 bytes, independent of larger
    # operation success-response budgets. Parse only after enforcing that bound.
    if type(body) is not bytes or not 1 <= len(body) <= 4096:
        return replace(result, rejection="body_size")
    try:
        document = json.loads(body.decode("utf-8", errors="strict"),
                              object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        return replace(result, rejection="invalid_json")
    if type(document) is not dict:
        return replace(result, rejection="invalid_fields")
    result = replace(result,
        problem_type=PROFILE["type"]["const"] if document.get("type") == PROFILE["type"]["const"] else None,
        title=PROFILE["title"]["const"] if document.get("title") == PROFILE["title"]["const"] else None,
        code=document.get("code") if type(document.get("code")) is str and document["code"] in PROFILE["code"]["enum"] else None,
        detail=_text(document.get("detail"), PROFILE["detail"]),
        body_request_id=_text(document.get("request_id"), PROFILE["request_id"]),
    )
    if set(document) != set(PROFILE):
        return replace(result, rejection="invalid_fields")
    if type(document["status"]) is not int or document["status"] != status or result.problem_type is None or result.title is None:
        return replace(result, rejection="invalid_identity")
    if result.code is None or result.detail is None:
        return replace(result, rejection="invalid_diagnostic")
    if result.body_request_id is None or result.header_request_id is None:
        return replace(result, rejection="invalid_request_id")
    # Source authority: nmr_api/security/problems.py:_send_problem correlates
    # header/body IDs and encodes this occurrence URN; OpenAPI cannot express it.
    expected_instance = "urn:nmr-api:request:" + quote(result.body_request_id, safe="")
    if _text(document["instance"], PROFILE["instance"]) != expected_instance:
        return replace(result, rejection="invalid_instance")
    result = replace(result, instance=expected_instance)
    if result.header_request_id != result.body_request_id:
        return replace(result, rejection="request_id_mismatch")
    return replace(result, verified=True, rejection=None)
