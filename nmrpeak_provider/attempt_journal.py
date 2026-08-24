"""Durable Attempt obligations and restart decisions, independent of storage."""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass, field, replace
from enum import Enum
from hashlib import sha256
import re

from .canonical_json import (
    CanonicalJsonError,
    JsonValue,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from .provider_https import ProviderOperation
from .provider_requests import (
    _PreparedProviderRequest,
    prepare_execution_attempt_complete,
    prepare_execution_attempt_fail,
)
from .provider_success import (
    AttemptState,
    ExecutionAttemptSnapshot,
    ExecutionAttemptStarted,
    JobState,
)


_SCHEMA_ID = "nmrpeak.attempt_journal_record.v1"
_JOB_REF = re.compile(r"job:[A-Za-z0-9_.-]{1,124}")
_ATTEMPT_KEY = re.compile(r"nmrpeak-provider\.v1:[0-9a-f]{64}")
_ATTEMPT_REF = re.compile(r"execution_attempt:sha256:[0-9a-f]{64}")
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
MAX_JOURNAL_RECORD_BYTES = 2_900_000


class LocalExecutionPhase(Enum):
    """The restart boundary around model execution."""

    PRE_EXECUTION = "pre_execution"
    EXECUTION_ENTERED = "execution_entered"


class TerminalOperation(Enum):
    """The one immutable terminal outcome selected for an Attempt."""

    COMPLETE = "complete"
    FAIL = "fail"


@dataclass(frozen=True, slots=True, kw_only=True)
class _AttemptRecord:
    job_ref: str
    provider_attempt_key: str
    input_fingerprint: str
    frozen_generation_id: str

    def __post_init__(self) -> None:
        _require_match(self.job_ref, _JOB_REF, "Job reference")
        _require_match(
            self.provider_attempt_key,
            _ATTEMPT_KEY,
            "provider Attempt key",
        )
        _require_match(
            self.input_fingerprint,
            _SHA256_REF,
            "input fingerprint",
        )
        _require_match(
            self.frozen_generation_id,
            _SHA256_REF,
            "frozen generation identity",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class StartPending(_AttemptRecord):
    """Stable start facts persisted before the first start send."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ActiveAttempt(_AttemptRecord):
    """A bound in-progress Attempt with its local execution boundary."""

    execution_attempt_ref: str
    local_phase: LocalExecutionPhase

    def __post_init__(self) -> None:
        _AttemptRecord.__post_init__(self)
        _require_match(
            self.execution_attempt_ref,
            _ATTEMPT_REF,
            "ExecutionAttempt reference",
        )
        if type(self.local_phase) is not LocalExecutionPhase:
            raise TypeError("Attempt journal local phase is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class TerminalPending(_AttemptRecord):
    """Exact terminal command bytes retained until receipt or expiry."""

    execution_attempt_ref: str
    terminal_operation: TerminalOperation
    terminal_request_body: bytes = field(repr=False)
    terminal_request_fingerprint: str

    def __post_init__(self) -> None:
        _AttemptRecord.__post_init__(self)
        _require_match(
            self.execution_attempt_ref,
            _ATTEMPT_REF,
            "ExecutionAttempt reference",
        )
        if type(self.terminal_operation) is not TerminalOperation:
            raise TypeError("Attempt journal terminal operation is invalid")
        if type(self.terminal_request_body) is not bytes:
            raise TypeError("Attempt journal terminal request must be exact bytes")
        expected_fingerprint = _fingerprint(self.terminal_request_body)
        if self.terminal_request_fingerprint != expected_fingerprint:
            raise ValueError("Attempt journal terminal request fingerprint has drifted")
        _validate_terminal_request(
            self.execution_attempt_ref,
            self.terminal_operation,
            self.terminal_request_body,
        )


AttemptJournalRecord = StartPending | ActiveAttempt | TerminalPending


def journal_record_name(record: AttemptJournalRecord) -> str:
    """Derive the safe filename from the record's stable Attempt key digest."""

    _require_record(record)
    digest = record.provider_attempt_key.removeprefix("nmrpeak-provider.v1:")
    return f"{digest}.json"


def journal_record_bytes(record: AttemptJournalRecord) -> bytes:
    """Render one closed canonical durable record without ambient facts."""

    _require_record(record)
    document: dict[str, JsonValue] = {
        "schema_id": _SCHEMA_ID,
        "record_kind": _record_kind(record),
        "job_ref": record.job_ref,
        "provider_attempt_key": record.provider_attempt_key,
        "input_fingerprint": record.input_fingerprint,
        "frozen_generation_id": record.frozen_generation_id,
    }
    if type(record) is ActiveAttempt:
        document |= {
            "execution_attempt_ref": record.execution_attempt_ref,
            "local_phase": record.local_phase.value,
        }
    elif type(record) is TerminalPending:
        document |= {
            "execution_attempt_ref": record.execution_attempt_ref,
            "terminal_operation": record.terminal_operation.value,
            "terminal_request_base64": b64encode(
                record.terminal_request_body
            ).decode("ascii"),
            "terminal_request_fingerprint": record.terminal_request_fingerprint,
        }
    encoded = canonical_json_bytes(document)
    if len(encoded) > MAX_JOURNAL_RECORD_BYTES:
        raise ValueError("Attempt journal record exceeds its durable size limit")
    return encoded


def parse_journal_record(raw: bytes) -> AttemptJournalRecord:
    """Admit one untrusted canonical record or fail the journal closed."""

    if type(raw) is not bytes:
        raise TypeError("Attempt journal record input must be exact bytes")
    if not raw or len(raw) > MAX_JOURNAL_RECORD_BYTES:
        raise ValueError("Attempt journal record has an invalid byte length")
    try:
        document = parse_canonical_json_bytes(raw)
    except CanonicalJsonError as error:
        raise ValueError("Attempt journal record is not canonical JSON") from error
    if type(document) is not dict or document.get("schema_id") != _SCHEMA_ID:
        raise ValueError("Attempt journal record schema is unsupported")
    kind = document.get("record_kind")
    try:
        if kind == "start_pending":
            _require_fields(document, _COMMON_FIELDS | {"record_kind"})
            record: AttemptJournalRecord = StartPending(**_common_values(document))
        elif kind == "active":
            _require_fields(
                document,
                _COMMON_FIELDS
                | {"record_kind", "execution_attempt_ref", "local_phase"},
            )
            record = ActiveAttempt(
                **_common_values(document),
                execution_attempt_ref=document["execution_attempt_ref"],
                local_phase=LocalExecutionPhase(document["local_phase"]),
            )
        elif kind == "terminal_pending":
            _require_fields(
                document,
                _COMMON_FIELDS
                | {
                    "record_kind",
                    "execution_attempt_ref",
                    "terminal_operation",
                    "terminal_request_base64",
                    "terminal_request_fingerprint",
                },
            )
            body_base64 = document["terminal_request_base64"]
            if type(body_base64) is not str:
                raise ValueError("Attempt journal terminal request is not base64 text")
            body = b64decode(body_base64, validate=True)
            if b64encode(body).decode("ascii") != body_base64:
                raise ValueError("Attempt journal terminal request base64 is not canonical")
            record = TerminalPending(
                **_common_values(document),
                execution_attempt_ref=document["execution_attempt_ref"],
                terminal_operation=TerminalOperation(document["terminal_operation"]),
                terminal_request_body=body,
                terminal_request_fingerprint=document[
                    "terminal_request_fingerprint"
                ],
            )
        else:
            raise ValueError("Attempt journal record kind is unsupported")
    except (KeyError, TypeError) as error:
        raise ValueError("Attempt journal record fields are invalid") from error
    if journal_record_bytes(record) != raw:
        raise ValueError("Attempt journal record canonical rendering has drifted")
    return record


def bind_started_attempt(
    record: StartPending,
    receipt: ExecutionAttemptStarted,
) -> ActiveAttempt:
    """Bind a validated in-progress start receipt to retained start facts."""

    if type(record) is not StartPending or type(receipt) is not ExecutionAttemptStarted:
        raise TypeError("Attempt start binding requires retained facts and a receipt")
    if receipt.job_ref != record.job_ref:
        raise ValueError("Attempt start receipt does not match the journal Job")
    if receipt.state is not AttemptState.IN_PROGRESS:
        raise ValueError("Attempt start receipt is already terminal")
    return ActiveAttempt(
        **_common_record_values(record),
        execution_attempt_ref=receipt.execution_attempt_ref,
        local_phase=LocalExecutionPhase.PRE_EXECUTION,
    )


def mark_execution_entered(record: ActiveAttempt) -> ActiveAttempt:
    """Persist the point after which restart must not rerun model execution."""

    if type(record) is not ActiveAttempt:
        raise TypeError("Execution entry requires an active Attempt record")
    if record.local_phase is not LocalExecutionPhase.PRE_EXECUTION:
        raise ValueError("Attempt execution has already been entered")
    return replace(record, local_phase=LocalExecutionPhase.EXECUTION_ENTERED)


def retain_terminal_command(
    record: ActiveAttempt,
    prepared: _PreparedProviderRequest,
) -> TerminalPending:
    """Replace local execution state with one exact terminal replay obligation."""

    if type(record) is not ActiveAttempt:
        raise TypeError("Terminal selection requires an active Attempt record")
    operation = {
        ProviderOperation.EXECUTION_ATTEMPT_COMPLETE: TerminalOperation.COMPLETE,
        ProviderOperation.EXECUTION_ATTEMPT_FAIL: TerminalOperation.FAIL,
    }.get(prepared.operation)
    if operation is None:
        raise ValueError("Attempt journal accepts only complete or fail commands")
    return TerminalPending(
        **_common_record_values(record),
        execution_attempt_ref=record.execution_attempt_ref,
        terminal_operation=operation,
        terminal_request_body=prepared.body,
        terminal_request_fingerprint=_fingerprint(prepared.body),
    )


def prepared_terminal_replay(record: TerminalPending) -> _PreparedProviderRequest:
    """Restore fixed route metadata around the exact retained terminal body."""

    if type(record) is not TerminalPending:
        raise TypeError("Terminal replay requires a retained terminal obligation")
    operation, path = {
        TerminalOperation.COMPLETE: (
            ProviderOperation.EXECUTION_ATTEMPT_COMPLETE,
            "/provider/v1/execution-attempts/complete",
        ),
        TerminalOperation.FAIL: (
            ProviderOperation.EXECUTION_ATTEMPT_FAIL,
            "/provider/v1/execution-attempts/fail",
        ),
    }[record.terminal_operation]
    return _PreparedProviderRequest(
        operation=operation,
        method="POST",
        path=path,
        query="",
        body=record.terminal_request_body,
    )


@dataclass(frozen=True, slots=True)
class ReplayStart:
    record: StartPending


@dataclass(frozen=True, slots=True)
class ResumePreExecution:
    record: ActiveAttempt


@dataclass(frozen=True, slots=True)
class PublishInterruptedFailure:
    record: ActiveAttempt
    failure_code: str = "provider_execution_interrupted"


@dataclass(frozen=True, slots=True)
class ObserveUntilExpiry:
    record: ActiveAttempt


@dataclass(frozen=True, slots=True)
class ReplayTerminal:
    record: TerminalPending


@dataclass(frozen=True, slots=True)
class RetainTerminalConflict:
    record: TerminalPending


@dataclass(frozen=True, slots=True)
class RetireResolved:
    record: ActiveAttempt | TerminalPending
    server_state: AttemptState


RestartDecision = (
    ReplayStart
    | ResumePreExecution
    | PublishInterruptedFailure
    | ObserveUntilExpiry
    | ReplayTerminal
    | RetainTerminalConflict
    | RetireResolved
)


def decide_restart(
    record: AttemptJournalRecord,
    snapshot: ExecutionAttemptSnapshot | None,
) -> RestartDecision:
    """Choose the only admitted restart action from durable and server facts."""

    _require_record(record)
    if type(record) is StartPending:
        if snapshot is not None:
            raise ValueError("A pending start cannot have a bound Attempt snapshot")
        return ReplayStart(record)
    if type(snapshot) is not ExecutionAttemptSnapshot:
        raise TypeError("A bound journal Attempt requires an authoritative snapshot")
    if (
        snapshot.execution_attempt_ref != record.execution_attempt_ref
        or snapshot.job_ref != record.job_ref
    ):
        raise ValueError("Attempt snapshot identity does not match the journal record")
    if type(record) is TerminalPending:
        return _terminal_restart(record, snapshot)
    if snapshot.state is not AttemptState.IN_PROGRESS:
        return RetireResolved(record, snapshot.state)
    if snapshot.job_state is not JobState.OPEN:
        return ObserveUntilExpiry(record)
    if record.local_phase is LocalExecutionPhase.PRE_EXECUTION:
        return ResumePreExecution(record)
    return PublishInterruptedFailure(record)


def _terminal_restart(
    record: TerminalPending,
    snapshot: ExecutionAttemptSnapshot,
) -> RestartDecision:
    if snapshot.state is AttemptState.EXPIRED:
        return RetireResolved(record, snapshot.state)
    expected_state = (
        AttemptState.SUCCEEDED
        if record.terminal_operation is TerminalOperation.COMPLETE
        else AttemptState.FAILED
    )
    if snapshot.state in {AttemptState.SUCCEEDED, AttemptState.FAILED}:
        if snapshot.state is not expected_state:
            return RetainTerminalConflict(record)
    return ReplayTerminal(record)


_COMMON_FIELDS = {
    "schema_id",
    "job_ref",
    "provider_attempt_key",
    "input_fingerprint",
    "frozen_generation_id",
}


def _record_kind(record: AttemptJournalRecord) -> str:
    if type(record) is StartPending:
        return "start_pending"
    if type(record) is ActiveAttempt:
        return "active"
    return "terminal_pending"


def _common_values(document: dict[str, JsonValue]) -> dict[str, object]:
    return {
        "job_ref": document["job_ref"],
        "provider_attempt_key": document["provider_attempt_key"],
        "input_fingerprint": document["input_fingerprint"],
        "frozen_generation_id": document["frozen_generation_id"],
    }


def _common_record_values(record: _AttemptRecord) -> dict[str, str]:
    return {
        "job_ref": record.job_ref,
        "provider_attempt_key": record.provider_attempt_key,
        "input_fingerprint": record.input_fingerprint,
        "frozen_generation_id": record.frozen_generation_id,
    }


def _require_fields(document: dict[str, JsonValue], expected: set[str]) -> None:
    if set(document) != expected:
        raise ValueError("Attempt journal record fields are invalid")


def _require_record(record: object) -> None:
    if type(record) not in {StartPending, ActiveAttempt, TerminalPending}:
        raise TypeError("Attempt journal operation requires a durable record")


def _require_match(
    value: object,
    pattern: re.Pattern[str],
    field: str,
) -> None:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"Attempt journal {field} has an invalid format")


def _fingerprint(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


def _validate_terminal_request(
    execution_attempt_ref: str,
    operation: TerminalOperation,
    body: bytes,
) -> None:
    try:
        document = parse_canonical_json_bytes(body)
        if type(document) is not dict:
            raise ValueError
        if operation is TerminalOperation.COMPLETE:
            encoded_result = document["canonical_result_base64"]
            if type(encoded_result) is not str:
                raise ValueError
            reconstructed = prepare_execution_attempt_complete(
                execution_attempt_ref=document["execution_attempt_ref"],
                result_schema_id=document["result_schema_id"],
                canonical_result=b64decode(encoded_result, validate=True),
            )
        else:
            reconstructed = prepare_execution_attempt_fail(
                execution_attempt_ref=document["execution_attempt_ref"],
                failure_code=document["failure_code"],
                failure_message=document["failure_message"],
            )
    except (CanonicalJsonError, KeyError, TypeError, ValueError) as error:
        raise ValueError("Attempt journal terminal request is invalid") from error
    if (
        reconstructed.operation
        is not {
            TerminalOperation.COMPLETE: ProviderOperation.EXECUTION_ATTEMPT_COMPLETE,
            TerminalOperation.FAIL: ProviderOperation.EXECUTION_ATTEMPT_FAIL,
        }[operation]
        or reconstructed.body != body
    ):
        raise ValueError("Attempt journal terminal request identity has drifted")
    if document["execution_attempt_ref"] != execution_attempt_ref:
        raise ValueError("Attempt journal terminal request targets another Attempt")
