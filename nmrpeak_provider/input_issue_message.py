"""Render product-owned input issues without reflecting submitted values."""

from __future__ import annotations

import re

from .failure_message import is_failure_message
from .product_input import MAX_JOB_INPUT_BYTES, InputIssue, InputRejectionReason


_SAFE_EXPECTED = re.compile(r"[A-Za-z0-9 ,;:_/\-]+", re.ASCII)


def _constraint(issue: InputIssue) -> str:
    reason = issue.reason
    if reason is InputRejectionReason.INVALID_UTF8:
        return "the input is not valid UTF-8 text"
    if reason is InputRejectionReason.EMPTY_INPUT:
        return "the submitted description is empty"
    if reason is InputRejectionReason.DISALLOWED_CONTROL:
        return "the submitted description contains a prohibited control character"
    if reason is InputRejectionReason.INVALID_JSON:
        if issue.line is not None and issue.column is not None:
            return f"JSON syntax is invalid at line {issue.line}, column {issue.column}"
        return "the input is not valid JSON"
    if reason is InputRejectionReason.DUPLICATE_FIELD:
        return "the JSON document contains a duplicate field"
    if reason is InputRejectionReason.DOCUMENT_TOO_LARGE:
        return f"the input exceeds this model's {MAX_JOB_INPUT_BYTES}-byte document limit"
    if reason is InputRejectionReason.INVALID_FORMULA:
        return "the molecular formula has invalid syntax"
    if reason is InputRejectionReason.UNSUPPORTED_MULTIPLICITY:
        return "the proton multiplicity label is unsupported by this model"
    if reason is InputRejectionReason.COUPLING_MUST_BE_NONNEGATIVE:
        return "the coupling must be nonnegative"
    if reason is InputRejectionReason.WRONG_SPECTRA:
        if issue.expected == "1H and 13C spectra":
            return "this model requires exactly a 1H spectrum and a 13C spectrum"
        if issue.expected == "1H spectrum":
            return "this model requires exactly a 1H spectrum"
        return "the required spectra for this model are missing or differ"
    if reason is InputRejectionReason.INVALID_STRUCTURE:
        expected = issue.expected
        if (
            expected is not None
            and len(expected) <= 160
            and _SAFE_EXPECTED.fullmatch(expected) is not None
        ):
            received = (
                f"; received {issue.received_type}"
                if issue.received_type in {
                    "JSON null", "JSON boolean", "JSON number",
                    "JSON string", "JSON array", "JSON object",
                    "a non-JSON value",
                }
                else ""
            )
            return f"expected {expected}{received}"
        return "the required NMRPeak input shape was not met"
    raise AssertionError("unhandled NMRPeak input issue")


def _location(issue: InputIssue) -> str:
    return f" at {issue.pointer}" if issue.pointer else ""


def render_source_issue(issue: InputIssue, *, structured: bool) -> str:
    """Publish a source-attributed diagnostic only for the direct Job bytes."""

    detail = _constraint(issue)
    format_help = (
        " If you meant prose beginning with '{' or '[', prefix it with ordinary words."
        if structured
        and issue.path == ()
        and issue.reason in {
            InputRejectionReason.INVALID_JSON,
            InputRejectionReason.INVALID_STRUCTURE,
        }
        and issue.expected not in {
            "UTF-8 text without a byte-order mark",
            "printable UTF-8 text after JSON whitespace",
        }
        else ""
    )
    message = (
        f"Input rejected{_location(issue)}: {detail}. "
        "Generation did not start. Correct the submitted Job and try again."
        + format_help
    )
    if not is_failure_message(message):
        raise ValueError("rendered source issue violates the Attempt message contract")
    return message


def render_candidate_correction(issue: InputIssue) -> str:
    """Give the assistant its rejected field/constraint without source text."""

    message = (
        f"The interpreted candidate{_location(issue)} was rejected: "
        f"{_constraint(issue)}. Correct the candidate using only facts in the "
        "submitted source; do not invent, omit, or substitute measurements."
    )
    if not is_failure_message(message):
        raise ValueError("rendered candidate issue violates the correction contract")
    return message


def render_candidate_failure(issue: InputIssue) -> str:
    """Describe exhausted model output without blaming the submitted source."""

    message = (
        f"Interpretation failed: the interpreter's candidate{_location(issue)} "
        f"was rejected after all correction routes: {_constraint(issue)}. "
        "Generation did not start. Review the submitted description; the "
        "candidate's defect has not been proven to occur in the source."
    )
    if not is_failure_message(message):
        raise ValueError("rendered candidate failure violates the Attempt contract")
    return message
