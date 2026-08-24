"""Parse and bind successful provider responses to their originating facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re

from .canonical_json import parse_canonical_json_bytes
from .provider_https import ProviderHttpResponse, ProviderOperation
from .provider_requests import _PreparedProviderRequest
from .provider_response_json import decode_provider_response_object


_PROVIDER_REF = re.compile(r"provider:[A-Za-z0-9_.-]{1,119}")
_JOB_REF = re.compile(r"job:[A-Za-z0-9_.-]{1,124}")
_ATTEMPT_REF = re.compile(r"execution_attempt:sha256:[0-9a-f]{64}")
_ANALYSIS_KIND = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_TIMESTAMP = re.compile(
    r"(?!0000)[0-9]{4}-(?:0[1-9]|1[0-2])-"
    r"(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):"
    r"[0-5][0-9]:[0-5][0-9](?:\.(?!000000)[0-9]{6})?Z"
)


class SuccessRejection(Enum):
    """Closed reasons a successful response cannot enter lifecycle state."""

    NOT_A_SUCCESS_RESPONSE = "not_a_success_response"
    INVALID_JSON = "invalid_json"
    INVALID_SHAPE = "invalid_shape"
    INVALID_FIELD = "invalid_field"
    RESPONSE_DRIFT = "response_drift"


@dataclass(frozen=True, slots=True)
class ProviderSuccessRejected:
    reason: SuccessRejection


class AttemptState(Enum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"


class JobState(Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ProviderHelloAccepted:
    provider_ref: str
    accepted_at: str


@dataclass(frozen=True, slots=True)
class ExecutionAttemptStarted:
    execution_attempt_ref: str
    job_ref: str
    analysis_kind_ref: str
    provider_ref: str
    state: AttemptState
    started_at: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ExecutionAttemptSnapshot:
    execution_attempt_ref: str
    job_ref: str
    state: AttemptState
    job_state: JobState


def parse_provider_hello_success(
    prepared: _PreparedProviderRequest,
    response: ProviderHttpResponse,
    *,
    expected_provider_ref: str,
) -> ProviderHelloAccepted | ProviderSuccessRejected:
    """Bind a hello acceptance to the configured provider identity."""

    _require_operation(prepared, ProviderOperation.PROVIDER_HELLO)
    _require_match(expected_provider_ref, _PROVIDER_REF, "expected provider reference")
    document = _success_document(response)
    if isinstance(document, ProviderSuccessRejected):
        return document
    if set(document) != {"schema_id", "provider_ref", "accepted_at"}:
        return ProviderSuccessRejected(SuccessRejection.INVALID_SHAPE)
    if document["schema_id"] != "nmr.provider.hello_response.v1":
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    provider_ref = document["provider_ref"]
    accepted_at = document["accepted_at"]
    if not _matches(provider_ref, _PROVIDER_REF) or not _is_timestamp(accepted_at):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    if provider_ref != expected_provider_ref:
        return ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT)
    return ProviderHelloAccepted(provider_ref, accepted_at)


def parse_execution_attempt_start_success(
    prepared: _PreparedProviderRequest,
    response: ProviderHttpResponse,
    *,
    expected_provider_ref: str,
    expected_analysis_kind_ref: str,
) -> ExecutionAttemptStarted | ProviderSuccessRejected:
    """Bind a start receipt to the Job, provider, and selected analysis lane."""

    _require_operation(prepared, ProviderOperation.EXECUTION_ATTEMPT_START)
    _require_match(expected_provider_ref, _PROVIDER_REF, "expected provider reference")
    _require_match(
        expected_analysis_kind_ref,
        _ANALYSIS_KIND,
        "expected analysis kind",
        maximum_characters=128,
    )
    document = _success_document(response)
    if isinstance(document, ProviderSuccessRejected):
        return document
    fields = {
        "schema_id",
        "execution_attempt_ref",
        "job_ref",
        "analysis_kind_ref",
        "provider_ref",
        "state",
        "started_at",
        "replayed",
    }
    if set(document) != fields:
        return ProviderSuccessRejected(SuccessRejection.INVALID_SHAPE)
    if document["schema_id"] != "nmr.provider.execution_attempt_start_response.v1":
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    state = _attempt_state(document["state"])
    if (
        not _matches(document["execution_attempt_ref"], _ATTEMPT_REF)
        or not _matches(document["job_ref"], _JOB_REF)
        or not _matches(document["analysis_kind_ref"], _ANALYSIS_KIND, 128)
        or not _matches(document["provider_ref"], _PROVIDER_REF)
        or state is None
        or not _is_timestamp(document["started_at"])
        or type(document["replayed"]) is not bool
    ):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    prepared_document = parse_canonical_json_bytes(prepared.body or b"")
    if type(prepared_document) is not dict:
        raise AssertionError("prepared start body must remain a canonical object")
    if (
        document["job_ref"] != prepared_document["job_ref"]
        or document["provider_ref"] != expected_provider_ref
        or document["analysis_kind_ref"] != expected_analysis_kind_ref
        or (not document["replayed"] and state is not AttemptState.IN_PROGRESS)
    ):
        return ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT)
    return ExecutionAttemptStarted(
        execution_attempt_ref=document["execution_attempt_ref"],
        job_ref=document["job_ref"],
        analysis_kind_ref=document["analysis_kind_ref"],
        provider_ref=document["provider_ref"],
        state=state,
        started_at=document["started_at"],
        replayed=document["replayed"],
    )


def parse_execution_attempt_read_success(
    prepared: _PreparedProviderRequest,
    response: ProviderHttpResponse,
    *,
    expected_job_ref: str,
) -> ExecutionAttemptSnapshot | ProviderSuccessRejected:
    """Bind an authoritative Attempt snapshot to its retained Job identity."""

    _require_operation(prepared, ProviderOperation.EXECUTION_ATTEMPT_READ)
    _require_match(expected_job_ref, _JOB_REF, "expected Job reference")
    document = _success_document(response)
    if isinstance(document, ProviderSuccessRejected):
        return document
    if set(document) != {
        "schema_id",
        "execution_attempt_ref",
        "job_ref",
        "state",
        "job_state",
    }:
        return ProviderSuccessRejected(SuccessRejection.INVALID_SHAPE)
    state = _attempt_state(document["state"])
    job_state = _job_state(document["job_state"])
    if (
        document["schema_id"] != "nmr.provider.execution_attempt_read_response.v1"
        or not _matches(document["execution_attempt_ref"], _ATTEMPT_REF)
        or not _matches(document["job_ref"], _JOB_REF)
        or state is None
        or job_state is None
    ):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    expected_attempt_ref = prepared.path.rsplit("/", 1)[1]
    if (
        document["execution_attempt_ref"] != expected_attempt_ref
        or document["job_ref"] != expected_job_ref
    ):
        return ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT)
    return ExecutionAttemptSnapshot(
        document["execution_attempt_ref"],
        document["job_ref"],
        state,
        job_state,
    )


def _success_document(
    response: ProviderHttpResponse,
) -> dict[str, object] | ProviderSuccessRejected:
    if (
        type(response) is not ProviderHttpResponse
        or response.status != 200
        or response.content_type != "application/json"
    ):
        return ProviderSuccessRejected(SuccessRejection.NOT_A_SUCCESS_RESPONSE)
    document = decode_provider_response_object(response.body)
    return (
        document
        if document is not None
        else ProviderSuccessRejected(SuccessRejection.INVALID_JSON)
    )


def _require_operation(
    prepared: _PreparedProviderRequest,
    operation: ProviderOperation,
) -> None:
    if type(prepared) is not _PreparedProviderRequest or prepared.operation is not operation:
        raise TypeError("Success parser requires its exact prepared operation")


def _require_match(
    value: object,
    pattern: re.Pattern[str],
    name: str,
    maximum_characters: int | None = None,
) -> None:
    if not _matches(value, pattern, maximum_characters):
        raise ValueError(f"{name} has an invalid format")


def _matches(
    value: object,
    pattern: re.Pattern[str],
    maximum_characters: int | None = None,
) -> bool:
    return (
        type(value) is str
        and (maximum_characters is None or len(value) <= maximum_characters)
        and pattern.fullmatch(value) is not None
    )


def _attempt_state(value: object) -> AttemptState | None:
    try:
        return AttemptState(value) if type(value) is str else None
    except ValueError:
        return None


def _job_state(value: object) -> JobState | None:
    try:
        return JobState(value) if type(value) is str else None
    except ValueError:
        return None


def _is_timestamp(value: object) -> bool:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True
