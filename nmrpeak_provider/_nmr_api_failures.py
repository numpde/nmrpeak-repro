"""Interpret current operation-specific Problems without owning recovery actions.

Only generated operation/status profiles are supported.
A verified refusal describes this response; it never resolves an earlier mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re
from urllib.parse import quote

from ._nmr_api_failure_contract import EVIDENCE, OPERATIONS, PROFILES, RECOVERY, SEND_EFFECTS


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
    upload_ref: str | None = field(default=None, repr=False)
    # Operation constraints, not retry permission or proof of any send outcome.
    recovery_mode: str | None = None
    recovery_description: str | None = None
    # This send only; a later refusal never clears uncertainty about earlier sends.
    current_send_effect: str | None = None


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
    indices = (OPERATIONS.get(operation, {}).get(status, ())
               if type(status) is int and type(operation) is str else ())
    result = FailureInterpretation(
        supported=bool(indices),
        verified=False, rejection="unsupported", status=status,
        header_request_id=_text(header_request_id, EVIDENCE["request_id"]),
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
    candidates = [PROFILES[index] for index in indices]
    selected = next((profile for profile in candidates
                     if document.get("type") == profile["properties"]["type"]["const"]), None)
    properties = selected["properties"] if selected is not None else None
    result = replace(result,
        problem_type=properties["type"]["const"] if properties is not None else None,
        title=(properties["title"]["const"] if properties is not None and
               document.get("title") == properties["title"]["const"] else None),
        code=(document["code"] if type(document.get("code")) is str and
              any(document["code"] in profile["properties"]["code"]["enum"]
                  for profile in candidates) else None),
        detail=_text(document.get("detail"), EVIDENCE["detail"]),
        body_request_id=_text(document.get("request_id"), EVIDENCE["request_id"]),
    )
    if selected is None:
        return replace(result, rejection="invalid_identity")
    required = set(properties) - {"upload_ref"}
    needs_upload = result.code in selected["upload_codes"]
    if set(document) != required | ({"upload_ref"} if needs_upload else set()):
        return replace(result, rejection="invalid_fields")
    if type(document["status"]) is not int or document["status"] != status or result.title is None:
        return replace(result, rejection="invalid_identity")
    if result.code not in properties["code"]["enum"] or result.detail is None:
        return replace(result, rejection="invalid_diagnostic")
    if (result.code in selected["fixed_details"] and
            result.detail != selected["fixed_details"][result.code]):
        return replace(result, rejection="invalid_diagnostic")
    if needs_upload:
        reference = document["upload_ref"]
        if type(reference) is not str or re.search(properties["upload_ref"]["pattern"], reference) is None:
            return replace(result, rejection="invalid_upload_ref")
        result = replace(result, upload_ref=reference)
    if result.body_request_id is None or result.header_request_id is None:
        return replace(result, rejection="invalid_request_id")
    # Source authority: nmr_api/security/problems.py:_send_problem correlates
    # header/body IDs and encodes this occurrence URN; OpenAPI cannot express it.
    expected_instance = "urn:nmr-api:request:" + quote(result.body_request_id, safe="")
    if _text(document["instance"], EVIDENCE["instance"]) != expected_instance:
        return replace(result, rejection="invalid_instance")
    result = replace(result, instance=expected_instance)
    if result.header_request_id != result.body_request_id:
        return replace(result, rejection="request_id_mismatch")
    return replace(result, verified=True, rejection=None,
                   recovery_mode=RECOVERY[operation]["mode"],
                   recovery_description=RECOVERY[operation]["description"],
                   current_send_effect=SEND_EFFECTS[operation][status][result.problem_type][result.code])
