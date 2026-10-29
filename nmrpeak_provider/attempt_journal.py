"""Read exact historical Attempt obligations without live recovery authority."""

from __future__ import annotations

from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import re

from .canonical_json import (
    CanonicalJsonError,
    JsonValue,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from .journal_evidence import CONFLICT_RECOVERY, EVIDENCE, admitted_evidence_text


_SCHEMA_ID_V1 = "nmrpeak.attempt_journal_record.v1"
_SCHEMA_ID_V2 = "nmrpeak.attempt_journal_record.v2"
_JOB_REF = re.compile(r"job:[A-Za-z0-9_.-]{1,124}")
_ATTEMPT_KEY = re.compile(r"nmrpeak-provider\.v1:[0-9a-f]{64}")
_ATTEMPT_REF = re.compile(r"execution_attempt:sha256:[0-9a-f]{64}")
_HISTORICAL_FAILURE_CODE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
MAX_JOURNAL_RECORD_BYTES = 2_900_000
_DIAGNOSTIC_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)
_ENDPOINT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_DIAGNOSTIC_STAGES = frozenset({"preparation"})
_DIAGNOSTIC_KINDS = frozenset({
    "direct_source_issue", "direct_runner_rejected", "model_reported_problem",
    "candidate_issue", "candidate_runner_rejected", "interpreter_unavailable",
})
_DIAGNOSTIC_PRODUCERS = frozenset({
    "provider", "runner", "interpreter", "interpreter_candidate",
})
# Reviewed, source-free reasons from InputRejectionReason,
# RunnerRejectionReason, InterpreterUnavailableReason, and the two fixed
# preparation outcomes. Keep this list explicit at the persistence boundary.
_DIAGNOSTIC_REASONS = frozenset({
    "document_too_large", "empty_input", "disallowed_control", "invalid_utf8",
    "invalid_json", "duplicate_field", "invalid_structure", "wrong_spectra",
    "invalid_formula", "unsupported_multiplicity", "coupling_must_be_nonnegative",
    "token_limit_exceeded", "tokenizer_empty_output", "dictionary_token_missing",
    "prompt_unavailable", "deadline_exceeded", "endpoints_exhausted",
    "model_report", "runner_candidate_rejected",
})
_DIAGNOSTIC_PATH_PARTS = frozenset({
    "schema_id", "model_input", "formula", "spectra", "1H", "13C", "peaks",
    "shift_lo", "shift_hi", "integral", "multiplicity", "j_hz", "shift",
})


class LocalExecutionPhase(Enum):
    """The restart boundary around model execution."""

    PRE_EXECUTION = "pre_execution"
    EXECUTION_ENTERED = "execution_entered"


class TerminalOperation(Enum):
    """The one immutable terminal outcome selected for an Attempt."""

    COMPLETE = "complete"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class LatestDiagnostic:
    """Bounded, source-free local evidence; never an API delivery receipt."""

    stage: str
    kind: str
    producer: str
    reason: str
    observed_at: str
    path: str | None = None
    endpoint_route: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.stage, self.kind, self.producer, self.reason):
            if type(value) is not str or _DIAGNOSTIC_CODE.fullmatch(value) is None:
                raise ValueError("Attempt diagnostic code is invalid")
        if (self.stage not in _DIAGNOSTIC_STAGES or self.kind not in _DIAGNOSTIC_KINDS
                or self.producer not in _DIAGNOSTIC_PRODUCERS):
            raise ValueError("Attempt diagnostic classification is not reviewed")
        if self.reason not in _DIAGNOSTIC_REASONS:
            raise ValueError("Attempt diagnostic reason is not reviewed")
        if self.path is not None:
            if type(self.path) is not str or not self.path.startswith("/") or len(self.path) > 256:
                raise ValueError("Attempt diagnostic path is invalid")
            if any(
                part not in _DIAGNOSTIC_PATH_PARTS and
                (not part.isascii() or not part.isdecimal() or len(part) > 6)
                for part in self.path.split("/")[1:]
            ):
                raise ValueError("Attempt diagnostic path contains an unowned segment")
        if type(self.endpoint_route) is not tuple or len(self.endpoint_route) > 4 or any(
            type(value) is not str or _ENDPOINT_ID.fullmatch(value) is None
            for value in self.endpoint_route
        ) or len(set(self.endpoint_route)) != len(self.endpoint_route):
            raise ValueError("Attempt diagnostic endpoint route is invalid")
        if type(self.observed_at) is not str or len(self.observed_at) > 40:
            raise ValueError("Attempt diagnostic timestamp is invalid")
        try:
            observed = datetime.fromisoformat(self.observed_at)
        except ValueError:
            raise ValueError("Attempt diagnostic timestamp is invalid") from None
        if observed.tzinfo is None or observed.utcoffset() != timezone.utc.utcoffset(observed):
            raise ValueError("Attempt diagnostic timestamp must be UTC")


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
        validate_frozen_generation_id(self.frozen_generation_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class StartPending(_AttemptRecord):
    """Stable start facts persisted before the first start send."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ActiveAttempt(_AttemptRecord):
    """A bound in-progress Attempt with its local execution boundary."""

    execution_attempt_ref: str
    local_phase: LocalExecutionPhase
    latest_diagnostic: LatestDiagnostic | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _AttemptRecord.__post_init__(self)
        _require_match(
            self.execution_attempt_ref,
            _ATTEMPT_REF,
            "ExecutionAttempt reference",
        )
        if type(self.local_phase) is not LocalExecutionPhase:
            raise TypeError("Attempt journal local phase is invalid")
        if self.latest_diagnostic is not None and type(self.latest_diagnostic) is not LatestDiagnostic:
            raise TypeError("Attempt journal latest diagnostic is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class TerminalPending(_AttemptRecord):
    """Exact terminal command bytes retained until a matching receipt."""

    execution_attempt_ref: str
    terminal_operation: TerminalOperation
    terminal_request_body: bytes = field(repr=False)
    terminal_request_fingerprint: str
    local_phase: LocalExecutionPhase | None = None
    terminal_hold_action: str | None = field(default=None)
    terminal_hold_description: str | None = field(default=None, repr=False)
    terminal_hold_code: str | None = None
    terminal_hold_detail: str | None = field(default=None, repr=False)
    terminal_hold_request_id: str | None = None
    terminal_observed_state: str | None = None
    latest_diagnostic: LatestDiagnostic | None = field(default=None, repr=False)

    @property
    def terminal_reconciling(self) -> bool:
        return self.terminal_hold_action == "reconcile_state" and self.terminal_observed_state is None

    def __post_init__(self) -> None:
        _AttemptRecord.__post_init__(self)
        _require_match(
            self.execution_attempt_ref,
            _ATTEMPT_REF,
            "ExecutionAttempt reference",
        )
        if self.local_phase is not None and type(self.local_phase) is not LocalExecutionPhase:
            raise TypeError("Terminal phase provenance is invalid")
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
        if (self.terminal_hold_action is None) != (self.terminal_hold_description is None):
            raise ValueError("Attempt journal hold requires action and description together")
        if self.terminal_hold_action is not None:
            if self.terminal_hold_action not in {"do_not_resend", "reconcile_original", "reconcile_state"}:
                raise ValueError("Attempt journal terminal hold action is invalid")
            if (type(self.terminal_hold_description) is not str or not self.terminal_hold_description
                    or len(self.terminal_hold_description.encode("utf-8")) > 4096
                    or not self.terminal_hold_description.isprintable()):
                raise ValueError("Attempt journal terminal hold description is invalid")

        evidence = (self.terminal_hold_code, self.terminal_hold_detail, self.terminal_hold_request_id)
        if self.terminal_hold_action is None and (any(value is not None for value in evidence) or self.terminal_observed_state is not None):
            raise ValueError("Attempt journal recovery evidence requires a hold")
        if any(value is not None for value in evidence):
            operation = "execution_attempt_" + self.terminal_operation.value
            if type(self.terminal_hold_code) is not str or self.terminal_hold_code not in CONFLICT_RECOVERY[operation]:
                raise ValueError("Attempt journal API conflict code is invalid for its operation")
            for value, name in ((self.terminal_hold_detail, "detail"), (self.terminal_hold_request_id, "request_id")):
                if admitted_evidence_text(value, EVIDENCE[name]) is None:
                    raise ValueError("Attempt journal API hold evidence is invalid")
        if self.terminal_observed_state not in {None, "in_progress", "succeeded", "failed", "expired", "not_visible"}:
            raise ValueError("Attempt journal observed state is invalid")
        if self.latest_diagnostic is not None and type(self.latest_diagnostic) is not LatestDiagnostic:
            raise TypeError("Attempt journal latest diagnostic is invalid")


AttemptJournalRecord = StartPending | ActiveAttempt | TerminalPending


def validate_frozen_generation_id(value: object) -> None:
    """Validate the journal's content-addressed generation reference."""

    _require_match(value, _SHA256_REF, "frozen generation identity")


def journal_record_name(record: AttemptJournalRecord) -> str:
    """Derive the safe filename from the record's stable Attempt key digest."""

    _require_record(record)
    digest = record.provider_attempt_key.removeprefix("nmrpeak-provider.v1:")
    return f"{digest}.json"


def journal_record_bytes(record: AttemptJournalRecord) -> bytes:
    """Render one closed canonical durable record without ambient facts."""

    _require_record(record)
    diagnostic = getattr(record, "latest_diagnostic", None)
    # Untouched legacy obligations keep their exact v1 bytes and digest. Only a
    # deliberately retained diagnostic promotes an active/terminal record to v2.
    document: dict[str, JsonValue] = {
        "schema_id": _SCHEMA_ID_V2 if diagnostic is not None else _SCHEMA_ID_V1,
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
        if record.local_phase is not None:
            document["local_phase"] = record.local_phase.value
        if record.terminal_hold_action is not None:
            document["record_kind"] = "terminal_reconciling" if record.terminal_reconciling else "terminal_hold"
            document["terminal_hold_action"] = record.terminal_hold_action
            document["terminal_hold_description"] = record.terminal_hold_description
            for name in ("terminal_hold_code", "terminal_hold_detail", "terminal_hold_request_id", "terminal_observed_state"):
                document[name] = getattr(record, name)
    if diagnostic is not None:
        document["latest_diagnostic"] = _diagnostic_document(diagnostic)
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
    if type(document) is not dict or type(document.get("schema_id")) is not str or document["schema_id"] not in {_SCHEMA_ID_V1, _SCHEMA_ID_V2}:
        raise ValueError("Attempt journal record schema is unsupported")
    has_diagnostic = document["schema_id"] == _SCHEMA_ID_V2
    kind = document.get("record_kind")
    try:
        if kind == "start_pending":
            if has_diagnostic:
                raise ValueError("Pending start cannot carry a latest diagnostic")
            _require_fields(document, _COMMON_FIELDS | {"record_kind"})
            record: AttemptJournalRecord = StartPending(**_common_values(document))
        elif kind == "active":
            _require_fields(
                document,
                _COMMON_FIELDS
                | {"record_kind", "execution_attempt_ref", "local_phase"}
                | ({"latest_diagnostic"} if has_diagnostic else set()),
            )
            record = ActiveAttempt(
                **_common_values(document),
                execution_attempt_ref=document["execution_attempt_ref"],
                local_phase=LocalExecutionPhase(document["local_phase"]),
                latest_diagnostic=_parse_diagnostic(document["latest_diagnostic"]) if has_diagnostic else None,
            )
        elif kind in {"terminal_pending", "terminal_hold", "terminal_reconciling"}:
            fields = _COMMON_FIELDS | {"record_kind", "execution_attempt_ref", "terminal_operation", "terminal_request_base64", "terminal_request_fingerprint"}
            if kind in {"terminal_hold", "terminal_reconciling"}:
                fields |= {"terminal_hold_action", "terminal_hold_description", "terminal_hold_code", "terminal_hold_detail", "terminal_hold_request_id", "terminal_observed_state"}
            if "local_phase" in document:
                fields.add("local_phase")
            if has_diagnostic:
                fields.add("latest_diagnostic")
            _require_fields(document, fields)
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
                local_phase=LocalExecutionPhase(document["local_phase"]) if "local_phase" in document else None,
                terminal_hold_action=document.get("terminal_hold_action"),
                terminal_hold_description=document.get("terminal_hold_description"),
                terminal_hold_code=document.get("terminal_hold_code"),
                terminal_hold_detail=document.get("terminal_hold_detail"),
                terminal_hold_request_id=document.get("terminal_hold_request_id"),
                terminal_observed_state=document.get("terminal_observed_state"),
                latest_diagnostic=_parse_diagnostic(document["latest_diagnostic"]) if has_diagnostic else None,
            )
        else:
            raise ValueError("Attempt journal record kind is unsupported")
    except (BinasciiError, KeyError, TypeError) as error:
        raise ValueError("Attempt journal record fields are invalid") from error
    if journal_record_bytes(record) != raw:
        raise ValueError("Attempt journal record canonical rendering has drifted")
    return record


def _diagnostic_document(value: LatestDiagnostic) -> dict[str, JsonValue]:
    return {
        "stage": value.stage,
        "kind": value.kind,
        "producer": value.producer,
        "reason": value.reason,
        "observed_at": value.observed_at,
        "path": value.path,
        "endpoint_route": list(value.endpoint_route),
    }


def _parse_diagnostic(value: object) -> LatestDiagnostic:
    if type(value) is not dict or set(value) != {
        "stage", "kind", "producer", "reason", "observed_at", "path", "endpoint_route"
    }:
        raise ValueError("Attempt journal latest diagnostic fields are invalid")
    route = value["endpoint_route"]
    if type(route) is not list:
        raise ValueError("Attempt journal latest diagnostic route is invalid")
    return LatestDiagnostic(
        stage=value["stage"], kind=value["kind"], producer=value["producer"],
        reason=value["reason"], observed_at=value["observed_at"],
        path=value["path"], endpoint_route=tuple(route),
    )


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


def _historical_text(value, maximum):
    if type(value) is not str or not value or len(value) > maximum or "\0" in value:
        raise ValueError("Historical terminal text is invalid")
    value.encode("utf-8", errors="strict")


def _validate_terminal_request(execution_attempt_ref, operation, body):
    """Admit exact historical command bytes without preparing a live API request."""
    try:
        document = parse_canonical_json_bytes(body)
        if operation is TerminalOperation.COMPLETE:
            _require_fields(document, {"schema_id", "execution_attempt_ref", "result_schema_id", "canonical_result_base64"})
            if document["schema_id"] != "nmr.provider.execution_attempt_complete_request.v1":
                raise ValueError
            _historical_text(document["result_schema_id"], 1024)
            encoded = document["canonical_result_base64"]
            if type(encoded) is not str:
                raise ValueError
            result = b64decode(encoded, validate=True)
            if not 1 <= len(result) <= 786432 or b64encode(result).decode("ascii") != encoded:
                raise ValueError
        else:
            _require_fields(document, {"schema_id", "execution_attempt_ref", "failure_code", "failure_message"})
            if document["schema_id"] != "nmr.provider.execution_attempt_fail_request.v1":
                raise ValueError
            _require_match(document["failure_code"], _HISTORICAL_FAILURE_CODE, "Historical failure code")
            if len(document["failure_code"]) > 128:
                raise ValueError
            _historical_text(document["failure_message"], 1024)
        if document["execution_attempt_ref"] != execution_attempt_ref or canonical_json_bytes(document) != body:
            raise ValueError
    except (CanonicalJsonError, KeyError, TypeError, ValueError) as cause:
        raise ValueError("Attempt journal terminal request is invalid") from cause
