"""Parse and bind successful provider responses to their originating facts."""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import re

from .canonical_json import parse_canonical_json_bytes
from .provider_https import ProviderHttpResponse, ProviderOperation
from .provider_requests import _PreparedProviderRequest
from .provider_response_json import decode_provider_response_object


_PROVIDER_REF = re.compile(r"provider:[A-Za-z0-9_.-]{1,119}")
_JOB_REF = re.compile(r"job:[A-Za-z0-9_.-]{1,124}")
_ATTEMPT_REF = re.compile(r"execution_attempt:sha256:[0-9a-f]{64}")
_ANALYSIS_RESULT_REF = re.compile(r"analysis_result:sha256:[0-9a-f]{64}")
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
_ANALYSIS_KIND = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_CONDITION_CODE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_ATTEMPT_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_CURSOR = re.compile(
    r"(?:[A-Za-z0-9_-]{4})*"
    r"(?:[A-Za-z0-9_-][AQgw]|[A-Za-z0-9_-]{2}[AEIMQUYcgkosw048]|"
    r"[A-Za-z0-9_-]{4})"
)
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


@dataclass(frozen=True, slots=True)
class ExecutionAttemptProgressed:
    execution_attempt_ref: str
    phase: str
    condition_code: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ExecutionAttemptCompleted:
    execution_attempt_ref: str
    analysis_result_ref: str
    committed_at: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ExecutionAttemptFailed:
    execution_attempt_ref: str
    committed_at: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class InProgressAttempt:
    analysis_kind_ref: str
    execution_attempt_ref: str
    job_ref: str
    provider_attempt_key: str
    started_at: str


@dataclass(frozen=True, slots=True)
class ExecutionAttemptsPage:
    attempts: tuple[InProgressAttempt, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class JobFeedItem:
    job_ref: str
    analysis_kind_ref: str
    input_fingerprint: str
    input_schema_id: str
    input_byte_length: int
    created_at: str


@dataclass(frozen=True, slots=True)
class JobsPage:
    analysis_kind_ref: str
    has_provider_execution_attempt: bool
    jobs: tuple[JobFeedItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class JobInput:
    job_ref: str
    input_fingerprint: str
    input_schema_id: str
    canonical_input: bytes


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


def parse_execution_attempt_progress_success(
    prepared: _PreparedProviderRequest,
    response: ProviderHttpResponse,
) -> ExecutionAttemptProgressed | ProviderSuccessRejected:
    """Accept progress only when the stored snapshot echoes the exact command."""

    _require_operation(prepared, ProviderOperation.EXECUTION_ATTEMPT_PROGRESS)
    document = _success_document(response)
    if isinstance(document, ProviderSuccessRejected):
        return document
    if set(document) != {
        "schema_id",
        "execution_attempt_ref",
        "phase",
        "condition_code",
        "updated_at",
    }:
        return ProviderSuccessRejected(SuccessRejection.INVALID_SHAPE)
    condition_code = document["condition_code"]
    if (
        document["schema_id"]
        != "nmr.provider.execution_attempt_progress_response.v1"
        or not _matches(document["execution_attempt_ref"], _ATTEMPT_REF)
        or type(document["phase"]) is not str
        or document["phase"] not in {"preparing", "running"}
        or not (
            condition_code is None
            or _matches(condition_code, _CONDITION_CODE, 128)
        )
        or not _is_timestamp(document["updated_at"])
    ):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    command = _prepared_document(prepared)
    expected_attempt_ref = prepared.path.split("/")[-2]
    if (
        document["execution_attempt_ref"] != expected_attempt_ref
        or document["phase"] != command["phase"]
        or condition_code != command["condition_code"]
    ):
        return ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT)
    return ExecutionAttemptProgressed(
        execution_attempt_ref=document["execution_attempt_ref"],
        phase=document["phase"],
        condition_code=condition_code,
        updated_at=document["updated_at"],
    )


def parse_execution_attempt_complete_success(
    prepared: _PreparedProviderRequest,
    response: ProviderHttpResponse,
) -> ExecutionAttemptCompleted | ProviderSuccessRejected:
    """Accept completion only when the receipt binds the exact retained result."""

    _require_operation(prepared, ProviderOperation.EXECUTION_ATTEMPT_COMPLETE)
    document = _success_document(response)
    if isinstance(document, ProviderSuccessRejected):
        return document
    if set(document) != {
        "schema_id",
        "execution_attempt_ref",
        "analysis_result_ref",
        "result_schema_id",
        "result_fingerprint",
        "result_byte_length",
        "committed_at",
        "replayed",
    }:
        return ProviderSuccessRejected(SuccessRejection.INVALID_SHAPE)
    if (
        document["schema_id"]
        != "nmr.provider.execution_attempt_complete_response.v1"
        or not _matches(document["execution_attempt_ref"], _ATTEMPT_REF)
        or not _matches(document["analysis_result_ref"], _ANALYSIS_RESULT_REF)
        or not _is_scalar_text(document["result_schema_id"], 1_024)
        or not _matches(document["result_fingerprint"], _SHA256_REF)
        or type(document["result_byte_length"]) is not int
        or not 1 <= document["result_byte_length"] <= 786_432
        or not _is_timestamp(document["committed_at"])
        or type(document["replayed"]) is not bool
    ):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    command = _prepared_document(prepared)
    result = b64decode(command["canonical_result_base64"], validate=True)
    expected_fingerprint = "sha256:" + sha256(result).hexdigest()
    if (
        document["execution_attempt_ref"] != command["execution_attempt_ref"]
        or document["result_schema_id"] != command["result_schema_id"]
        or document["result_fingerprint"] != expected_fingerprint
        or document["result_byte_length"] != len(result)
    ):
        return ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT)
    return ExecutionAttemptCompleted(
        execution_attempt_ref=document["execution_attempt_ref"],
        analysis_result_ref=document["analysis_result_ref"],
        committed_at=document["committed_at"],
        replayed=document["replayed"],
    )


def parse_execution_attempt_fail_success(
    prepared: _PreparedProviderRequest,
    response: ProviderHttpResponse,
) -> ExecutionAttemptFailed | ProviderSuccessRejected:
    """Accept failure only when the receipt echoes the reviewed command facts."""

    _require_operation(prepared, ProviderOperation.EXECUTION_ATTEMPT_FAIL)
    document = _success_document(response)
    if isinstance(document, ProviderSuccessRejected):
        return document
    if set(document) != {
        "schema_id",
        "execution_attempt_ref",
        "failure_code",
        "failure_message",
        "committed_at",
        "replayed",
    }:
        return ProviderSuccessRejected(SuccessRejection.INVALID_SHAPE)
    if (
        document["schema_id"] != "nmr.provider.execution_attempt_fail_response.v1"
        or not _matches(document["execution_attempt_ref"], _ATTEMPT_REF)
        or not _matches(document["failure_code"], _CONDITION_CODE, 128)
        or not _is_scalar_text(document["failure_message"], 1_024)
        or not _is_timestamp(document["committed_at"])
        or type(document["replayed"]) is not bool
    ):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    command = _prepared_document(prepared)
    if any(
        document[field] != command[field]
        for field in ("execution_attempt_ref", "failure_code", "failure_message")
    ):
        return ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT)
    return ExecutionAttemptFailed(
        execution_attempt_ref=document["execution_attempt_ref"],
        committed_at=document["committed_at"],
        replayed=document["replayed"],
    )


def parse_execution_attempts_list_success(
    prepared: _PreparedProviderRequest,
    response: ProviderHttpResponse,
) -> ExecutionAttemptsPage | ProviderSuccessRejected:
    """Parse one bounded page of the provider's live Attempt inventory."""

    _require_operation(prepared, ProviderOperation.EXECUTION_ATTEMPTS_LIST)
    document = _success_document(response)
    if isinstance(document, ProviderSuccessRejected):
        return document
    if set(document) != {"schema_id", "attempts", "next_cursor"}:
        return ProviderSuccessRejected(SuccessRejection.INVALID_SHAPE)
    attempts = document["attempts"]
    next_cursor = document["next_cursor"]
    if (
        document["schema_id"]
        != "nmr.provider.execution_attempts.list.response.v1"
        or type(attempts) is not list
        or len(attempts) > _prepared_page_limit(prepared)
        or not _is_cursor_or_none(next_cursor)
    ):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    parsed = tuple(_in_progress_attempt(item) for item in attempts)
    if any(item is None for item in parsed):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    return ExecutionAttemptsPage(parsed, next_cursor)


def parse_jobs_list_success(
    prepared: _PreparedProviderRequest,
    response: ProviderHttpResponse,
) -> JobsPage | ProviderSuccessRejected:
    """Bind one Job page to the exact selected analysis and Attempt filter."""

    _require_operation(prepared, ProviderOperation.JOBS_LIST)
    document = _success_document(response)
    if isinstance(document, ProviderSuccessRejected):
        return document
    if set(document) != {
        "schema_id",
        "analysis_kind_ref",
        "has_provider_execution_attempt",
        "jobs",
        "next_cursor",
    }:
        return ProviderSuccessRejected(SuccessRejection.INVALID_SHAPE)
    jobs = document["jobs"]
    next_cursor = document["next_cursor"]
    if (
        document["schema_id"] != "nmr.provider.jobs.list.response.v1"
        or not _matches(document["analysis_kind_ref"], _ANALYSIS_KIND, 128)
        or type(document["has_provider_execution_attempt"]) is not bool
        or type(jobs) is not list
        or len(jobs) > _prepared_page_limit(prepared)
        or not _is_cursor_or_none(next_cursor)
    ):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    query = _prepared_query(prepared)
    expected_analysis_kind = query["analysis_kind_ref"]
    expected_attempt_filter = query.get("has_provider_execution_attempt", "false")
    if (
        document["analysis_kind_ref"] != expected_analysis_kind
        or document["has_provider_execution_attempt"]
        != (expected_attempt_filter == "true")
    ):
        return ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT)
    parsed = tuple(_job_feed_item(item) for item in jobs)
    if any(item is None for item in parsed):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    if any(item.analysis_kind_ref != expected_analysis_kind for item in parsed):
        return ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT)
    return JobsPage(
        analysis_kind_ref=document["analysis_kind_ref"],
        has_provider_execution_attempt=document["has_provider_execution_attempt"],
        jobs=parsed,
        next_cursor=next_cursor,
    )


def parse_job_input_read_success(
    prepared: _PreparedProviderRequest,
    response: ProviderHttpResponse,
    *,
    expected_job: JobFeedItem,
) -> JobInput | ProviderSuccessRejected:
    """Verify exact Job input bytes against every feed-supplied identity fact."""

    _require_operation(prepared, ProviderOperation.JOB_INPUT_READ)
    if type(expected_job) is not JobFeedItem:
        raise TypeError("Job input parsing requires an exact feed item")
    requested_job_ref = prepared.path.split("/")[-2]
    requested_analysis_kind = _prepared_query(prepared)["analysis_kind_ref"]
    if (
        expected_job.job_ref != requested_job_ref
        or expected_job.analysis_kind_ref != requested_analysis_kind
    ):
        raise ValueError("Job input feed identity does not match the prepared read")
    document = _success_document(response)
    if isinstance(document, ProviderSuccessRejected):
        return document
    if set(document) != {
        "schema_id",
        "job_ref",
        "input_fingerprint",
        "input_schema_id",
        "input_byte_length",
        "canonical_input_base64",
    }:
        return ProviderSuccessRejected(SuccessRejection.INVALID_SHAPE)
    encoded = document["canonical_input_base64"]
    if (
        document["schema_id"] != "nmr.provider.job_input.read.response.v1"
        or not _matches(document["job_ref"], _JOB_REF)
        or not _matches(document["input_fingerprint"], _SHA256_REF)
        or document["input_schema_id"] != "nmr.job.specification.text.v1"
        or type(document["input_byte_length"]) is not int
        or not 1 <= document["input_byte_length"] <= 65_536
        or type(encoded) is not str
        or not 1 <= len(encoded) <= 87_384
    ):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    try:
        canonical_input = b64decode(encoded, validate=True)
        canonical_input.decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    if b64encode(canonical_input).decode("ascii") != encoded:
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    expected_fingerprint = "sha256:" + sha256(canonical_input).hexdigest()
    if (
        len(canonical_input) != document["input_byte_length"]
        or expected_fingerprint != document["input_fingerprint"]
    ):
        return ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
    if (
        document["job_ref"] != expected_job.job_ref
        or document["input_fingerprint"] != expected_job.input_fingerprint
        or document["input_schema_id"] != expected_job.input_schema_id
        or document["input_byte_length"] != expected_job.input_byte_length
    ):
        return ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT)
    return JobInput(
        job_ref=document["job_ref"],
        input_fingerprint=document["input_fingerprint"],
        input_schema_id=document["input_schema_id"],
        canonical_input=canonical_input,
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


def _prepared_document(prepared: _PreparedProviderRequest) -> dict[str, object]:
    document = parse_canonical_json_bytes(prepared.body or b"")
    if type(document) is not dict:
        raise AssertionError("prepared command body must remain a canonical object")
    return document


def _prepared_query(prepared: _PreparedProviderRequest) -> dict[str, str]:
    return dict(field.split("=", 1) for field in prepared.query.split("&"))


def _prepared_page_limit(prepared: _PreparedProviderRequest) -> int:
    return int(_prepared_query(prepared).get("limit", "50"))


def _is_cursor_or_none(value: object) -> bool:
    return value is None or _matches(value, _CURSOR)


def _in_progress_attempt(value: object) -> InProgressAttempt | None:
    if type(value) is not dict or set(value) != {
        "analysis_kind_ref",
        "execution_attempt_ref",
        "job_ref",
        "provider_attempt_key",
        "state",
        "started_at",
    }:
        return None
    if (
        not _matches(value["analysis_kind_ref"], _ANALYSIS_KIND, 128)
        or not _matches(value["execution_attempt_ref"], _ATTEMPT_REF)
        or not _matches(value["job_ref"], _JOB_REF)
        or not _matches(value["provider_attempt_key"], _ATTEMPT_KEY)
        or value["state"] != "in_progress"
        or not _is_timestamp(value["started_at"])
    ):
        return None
    return InProgressAttempt(
        value["analysis_kind_ref"],
        value["execution_attempt_ref"],
        value["job_ref"],
        value["provider_attempt_key"],
        value["started_at"],
    )


def _job_feed_item(value: object) -> JobFeedItem | None:
    if type(value) is not dict or set(value) != {
        "job_ref",
        "analysis_kind_ref",
        "input_fingerprint",
        "input_schema_id",
        "input_byte_length",
        "created_at",
    }:
        return None
    if (
        not _matches(value["job_ref"], _JOB_REF)
        or not _matches(value["analysis_kind_ref"], _ANALYSIS_KIND, 128)
        or not _matches(value["input_fingerprint"], _SHA256_REF)
        or value["input_schema_id"] != "nmr.job.specification.text.v1"
        or type(value["input_byte_length"]) is not int
        or not 1 <= value["input_byte_length"] <= 65_536
        or not _is_timestamp(value["created_at"])
    ):
        return None
    return JobFeedItem(
        value["job_ref"],
        value["analysis_kind_ref"],
        value["input_fingerprint"],
        value["input_schema_id"],
        value["input_byte_length"],
        value["created_at"],
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


def _is_scalar_text(value: object, maximum_characters: int) -> bool:
    if type(value) is not str or not 1 <= len(value) <= maximum_characters:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return "\0" not in value
