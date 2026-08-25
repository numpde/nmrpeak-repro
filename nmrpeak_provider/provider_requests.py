"""Prepare the nine provider operations from typed lifecycle facts."""

from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .canonical_json import JsonValue, canonical_json_bytes
from .provider_https import ProviderOperation
from .provider_signing import SignedProviderRequest, sign_provider_request


_ANALYSIS_KIND = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_JOB_REF = re.compile(r"job:[A-Za-z0-9_.-]{1,124}")
_ATTEMPT_REF = re.compile(r"execution_attempt:sha256:[0-9a-f]{64}")
_ATTEMPT_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_CONDITION_CODE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_CURSOR = re.compile(
    r"(?:[A-Za-z0-9_-]{4})*"
    r"(?:[A-Za-z0-9_-][AQgw]|[A-Za-z0-9_-]{2}[AEIMQUYcgkosw048]|"
    r"[A-Za-z0-9_-]{4})"
)
@dataclass(frozen=True, slots=True)
class _PreparedProviderRequest:
    """One operation's exact unsigned target and canonical body bytes."""

    operation: ProviderOperation
    method: str
    path: str
    query: str
    body: bytes | None


@dataclass(frozen=True, slots=True)
class HelloOffering:
    """One analysis description published in the complete hello snapshot."""

    analysis_kind_ref: str
    description: str


def prepare_execution_attempts_list(
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> _PreparedProviderRequest:
    """Prepare one page of the fixed in-progress Attempt inventory."""

    query = ["state=in_progress"]
    _append_page_fields(query, limit=limit, cursor=cursor)
    return _bodyless(
        ProviderOperation.EXECUTION_ATTEMPTS_LIST,
        path="/provider/v1/execution-attempts",
        query="&".join(query),
    )


def prepare_jobs_list(
    *,
    analysis_kind_ref: str,
    has_provider_execution_attempt: bool | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> _PreparedProviderRequest:
    """Prepare one Job feed page in the contract's exact query order."""

    _require_match(analysis_kind_ref, _ANALYSIS_KIND, "analysis kind", 128)
    query = [f"analysis_kind_ref={analysis_kind_ref}"]
    if has_provider_execution_attempt is not None:
        if type(has_provider_execution_attempt) is not bool:
            raise TypeError("Job feed Attempt filter must be a boolean or absent")
        encoded = "true" if has_provider_execution_attempt else "false"
        query.append(f"has_provider_execution_attempt={encoded}")
    _append_page_fields(query, limit=limit, cursor=cursor)
    return _bodyless(
        ProviderOperation.JOBS_LIST,
        path="/provider/v1/jobs",
        query="&".join(query),
    )


def prepare_job_input_read(
    *,
    job_ref: str,
    analysis_kind_ref: str,
) -> _PreparedProviderRequest:
    """Prepare the exact immutable Job-input read target."""

    _require_match(job_ref, _JOB_REF, "Job reference")
    _require_match(analysis_kind_ref, _ANALYSIS_KIND, "analysis kind", 128)
    return _bodyless(
        ProviderOperation.JOB_INPUT_READ,
        path=f"/provider/v1/jobs/{job_ref}/input",
        query=f"analysis_kind_ref={analysis_kind_ref}",
    )


def prepare_execution_attempt_read(
    execution_attempt_ref: str,
) -> _PreparedProviderRequest:
    """Prepare an authoritative point read of one ExecutionAttempt."""

    _require_match(
        execution_attempt_ref,
        _ATTEMPT_REF,
        "ExecutionAttempt reference",
    )
    return _bodyless(
        ProviderOperation.EXECUTION_ATTEMPT_READ,
        path=f"/provider/v1/execution-attempts/{execution_attempt_ref}",
        query="",
    )


def prepare_execution_attempt_start(
    *,
    job_ref: str,
    provider_attempt_key: str,
) -> _PreparedProviderRequest:
    """Prepare the stable idempotent command that starts one Attempt."""

    _require_match(job_ref, _JOB_REF, "Job reference")
    _require_match(provider_attempt_key, _ATTEMPT_KEY, "provider Attempt key")
    return _command(
        ProviderOperation.EXECUTION_ATTEMPT_START,
        path="/provider/v1/execution-attempts/start",
        document={
            "schema_id": "nmr.provider.execution_attempt_start_request.v1",
            "job_ref": job_ref,
            "provider_attempt_key": provider_attempt_key,
        },
    )


def prepare_execution_attempt_progress(
    *,
    execution_attempt_ref: str,
    phase: str,
    condition_code: str | None,
) -> _PreparedProviderRequest:
    """Prepare one complete provider progress observation."""

    _require_match(
        execution_attempt_ref,
        _ATTEMPT_REF,
        "ExecutionAttempt reference",
    )
    if type(phase) is not str or phase not in {"preparing", "running"}:
        raise ValueError("Attempt progress phase must be preparing or running")
    if condition_code is not None:
        _require_match(
            condition_code,
            _CONDITION_CODE,
            "Attempt progress condition code",
            128,
        )
    return _command(
        ProviderOperation.EXECUTION_ATTEMPT_PROGRESS,
        path=f"/provider/v1/execution-attempts/{execution_attempt_ref}/progress",
        document={
            "schema_id": "nmr.provider.execution_attempt_progress_request.v1",
            "phase": phase,
            "condition_code": condition_code,
        },
    )


def prepare_execution_attempt_complete(
    *,
    execution_attempt_ref: str,
    result_schema_id: str,
    canonical_result: bytes,
) -> _PreparedProviderRequest:
    """Prepare the exact success command that the journal must retain."""

    _require_match(
        execution_attempt_ref,
        _ATTEMPT_REF,
        "ExecutionAttempt reference",
    )
    _require_bounded_text(result_schema_id, "result schema identity", 1_024)
    if type(canonical_result) is not bytes:
        raise TypeError("Attempt completion result must be exact bytes")
    if not 1 <= len(canonical_result) <= 786_432:
        raise ValueError(
            "Attempt completion requires 1 to 786432 exact result bytes"
        )
    return _command(
        ProviderOperation.EXECUTION_ATTEMPT_COMPLETE,
        path="/provider/v1/execution-attempts/complete",
        document={
            "schema_id": "nmr.provider.execution_attempt_complete_request.v1",
            "execution_attempt_ref": execution_attempt_ref,
            "result_schema_id": result_schema_id,
            "canonical_result_base64": b64encode(canonical_result).decode("ascii"),
        },
    )


def prepare_execution_attempt_fail(
    *,
    execution_attempt_ref: str,
    failure_code: str,
    failure_message: str,
) -> _PreparedProviderRequest:
    """Prepare one reviewed non-sensitive scientific failure command."""

    _require_match(
        execution_attempt_ref,
        _ATTEMPT_REF,
        "ExecutionAttempt reference",
    )
    _require_match(failure_code, _CONDITION_CODE, "Attempt failure code", 128)
    _require_bounded_text(failure_message, "Attempt failure message", 1_024)
    return _command(
        ProviderOperation.EXECUTION_ATTEMPT_FAIL,
        path="/provider/v1/execution-attempts/fail",
        document={
            "schema_id": "nmr.provider.execution_attempt_fail_request.v1",
            "execution_attempt_ref": execution_attempt_ref,
            "failure_code": failure_code,
            "failure_message": failure_message,
        },
    )


def prepare_provider_hello(
    *,
    display_name: str,
    description: str,
    analysis_offerings: tuple[HelloOffering, ...],
) -> _PreparedProviderRequest:
    """Prepare one complete replacement snapshot of provider presentation."""

    _require_bounded_text(display_name, "provider display name", 128)
    _require_bounded_text(description, "provider description", 1_024)
    if type(analysis_offerings) is not tuple or len(analysis_offerings) > 64:
        raise ValueError("Provider hello requires a tuple of at most 64 offerings")
    offerings: list[JsonValue] = []
    for offering in analysis_offerings:
        if type(offering) is not HelloOffering:
            raise TypeError("Provider hello offerings must be exact offering facts")
        _require_match(
            offering.analysis_kind_ref,
            _ANALYSIS_KIND,
            "analysis kind",
            128,
        )
        _require_bounded_text(
            offering.description,
            "analysis offering description",
            1_024,
        )
        offerings.append(
            {
                "analysis_kind_ref": offering.analysis_kind_ref,
                "description": offering.description,
            }
        )
    return _command(
        ProviderOperation.PROVIDER_HELLO,
        path="/provider/v1/hello",
        document={
            "schema_id": "nmr.provider.hello_request.v1",
            "display_name": display_name,
            "description": description,
            "analysis_offerings": offerings,
        },
    )


def sign_prepared_provider_request(
    prepared: _PreparedProviderRequest,
    *,
    private_key: Ed25519PrivateKey,
    credential_ref: str,
    authority: str,
    created: int,
    nonce: bytes,
) -> SignedProviderRequest:
    """Add one fresh signature without changing prepared business bytes."""

    if type(prepared) is not _PreparedProviderRequest:
        raise TypeError("Provider signing requires an exact prepared request")
    return sign_provider_request(
        private_key=private_key,
        credential_ref=credential_ref,
        method=prepared.method,
        authority=authority,
        path=prepared.path,
        query=prepared.query,
        body=prepared.body,
        created=created,
        nonce=nonce,
    )


def _bodyless(
    operation: ProviderOperation,
    *,
    path: str,
    query: str,
) -> _PreparedProviderRequest:
    return _PreparedProviderRequest(
        operation,
        "GET",
        path,
        query,
        None,
    )


def _command(
    operation: ProviderOperation,
    *,
    path: str,
    document: dict[str, JsonValue],
) -> _PreparedProviderRequest:
    method = (
        "PUT"
        if operation is ProviderOperation.EXECUTION_ATTEMPT_PROGRESS
        else "POST"
    )
    body = canonical_json_bytes(document)
    return _PreparedProviderRequest(
        operation,
        method,
        path,
        "",
        body,
    )


def _append_page_fields(
    fields: list[str],
    *,
    limit: int | None,
    cursor: str | None,
) -> None:
    if limit is not None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Provider page limit must be an integer from 1 to 100")
        fields.append(f"limit={limit}")
    if cursor is not None:
        _require_match(cursor, _CURSOR, "provider page cursor")
        fields.append(f"cursor={cursor}")


def _require_match(
    value: object,
    pattern: re.Pattern[str],
    name: str,
    maximum_characters: int | None = None,
) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    if (
        (maximum_characters is not None and len(value) > maximum_characters)
        or pattern.fullmatch(value) is None
    ):
        raise ValueError(f"{name} has an invalid format")


def _require_bounded_text(value: object, name: str, maximum_characters: int) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must contain Unicode scalar text") from error
    if not value or len(value) > maximum_characters or "\0" in value:
        raise ValueError(f"{name} must be non-empty bounded text without NUL")
