"""Closed, payload-free inspection document shared by producer and deployment reader."""
from collections import Counter
from datetime import datetime
import re

from ._nmr_api_failure_contract import CONFLICT_RECOVERY, EVIDENCE
from ._nmr_api_failures import _text as admitted_evidence_text
from .attempt_journal import LatestDiagnostic
from .failure_message import is_failure_message


def _fields(value, expected):
    if type(value) is not dict or set(value) != set(expected.split()):
        raise ValueError("Invalid inspection fields")


def _text(value, maximum=8192):
    if type(value) is not str or not value or not value.isprintable() or len(value.encode("utf-8")) > maximum:
        raise ValueError("Invalid inspection text")


def _identity(value, pattern):
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        raise ValueError("Invalid inspection identity")


def validate_inspection_document(document):
    """Reject unknown fields, payloads, inconsistent identities and recovery claims."""
    _fields(document, "schema_id current_automation observed_at stage_counts records")
    schema = document["schema_id"]
    if type(schema) is not str or schema not in {"nmrpeak.journal_inspection.v1", "nmrpeak.journal_inspection.v2"} or document["current_automation"] != "stopped":
        raise ValueError("Invalid inspection envelope")
    _text(document["observed_at"], 64)
    observed = datetime.fromisoformat(document["observed_at"])
    if observed.utcoffset() is None:
        raise ValueError("Inspection observation requires a timezone")
    records = document["records"]
    if type(records) is not list or len(records) > 10000:
        raise ValueError("Invalid inspection records")
    for record in records:
        _validate_record(record, version=1 if schema.endswith(".v1") else 2)
    counts = document["stage_counts"]
    if (type(counts) is not dict or any(type(count) is not int or count <= 0 for count in counts.values())
            or counts != dict(Counter(record["phase"] for record in records))):
        raise ValueError("Invalid inspection stage counts")
    identities = [record["provider_attempt_key"] for record in records]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate inspection obligation")


def _validate_record(record, *, version):
    if type(record) is not dict:
        raise ValueError("Invalid inspection record")
    phase = record.get("phase")
    common = "record_digest phase job_ref provider_attempt_key frozen_generation_id next_actor next_action restart_behavior"
    extra = {
        "start_pending": "delivery",
        "active": "execution_attempt_ref local_phase",
        "terminal_pending": "execution_attempt_ref operation delivery command_fingerprint command_byte_count",
        "terminal_reconciling": "execution_attempt_ref operation delivery command_fingerprint command_byte_count recovery on_restart",
        "terminal_hold": "execution_attempt_ref operation delivery command_fingerprint command_byte_count recovery on_restart",
    }
    if type(phase) is not str or phase not in extra:
        raise ValueError("Invalid inspection phase")
    expected = common + " " + extra[phase]
    if version == 2 and phase != "start_pending":
        expected += " latest_diagnostic"
        if phase != "active" and record.get("operation") == "fail":
            expected += " retained_failure"
    _fields(record, expected)
    _identity(record["record_digest"], r"sha256:[0-9a-f]{64}")
    _identity(record["job_ref"], r"job:[A-Za-z0-9_.-]{1,124}")
    _identity(record["provider_attempt_key"], r"nmrpeak-provider\.v1:[0-9a-f]{64}")
    _identity(record["frozen_generation_id"], r"sha256:[0-9a-f]{64}")
    for name in ("next_action", "restart_behavior"):
        _text(record[name])
    if record["next_actor"] != "provider_operator":
        raise ValueError("Inspection requires operator action")
    if "delivery" in record and record["delivery"] != "unconfirmed":
        raise ValueError("Inspection cannot assert delivery")
    if phase == "start_pending":
        return
    _identity(record["execution_attempt_ref"], r"execution_attempt:sha256:[0-9a-f]{64}")
    if version == 2:
        _validate_latest_diagnostic(record["latest_diagnostic"])
    if phase == "active":
        if record["local_phase"] not in ("pre_execution", "execution_entered"):
            raise ValueError("Invalid local execution phase")
        return
    if record["operation"] not in ("complete", "fail"):
        raise ValueError("Invalid terminal operation")
    if version == 2 and record["operation"] == "fail":
        failure = record["retained_failure"]
        _fields(failure, "failure_code failure_message")
        _identity(failure["failure_code"], r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
        if len(failure["failure_code"]) > 128 or not is_failure_message(failure["failure_message"]):
            raise ValueError("Invalid retained failure")
    _identity(record["command_fingerprint"], r"sha256:[0-9a-f]{64}")
    size = record["command_byte_count"]
    if type(size) is not int or not 0 < size <= 2900000:
        raise ValueError("Invalid retained command size")
    if phase != "terminal_pending":
        _validate_recovery(record)


def _validate_latest_diagnostic(value):
    if value is None:
        return
    _fields(value, "stage kind producer reason observed_at path endpoint_route")
    route = value["endpoint_route"]
    if type(route) is not list:
        raise ValueError("Invalid latest diagnostic route")
    try:
        LatestDiagnostic(
            stage=value["stage"], kind=value["kind"], producer=value["producer"],
            reason=value["reason"], observed_at=value["observed_at"],
            path=value["path"], endpoint_route=tuple(route),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid latest diagnostic") from error


def _validate_recovery(record):
    recovery, restart = record["recovery"], record["on_restart"]
    _fields(recovery, "job_ref execution_attempt_ref operation command_fingerprint command_retained delivery action description code detail request_id observed_state")
    for name in ("job_ref", "execution_attempt_ref", "operation", "command_fingerprint", "delivery"):
        if recovery[name] != record[name]:
            raise ValueError("Inconsistent recovery identity")
    if recovery["command_retained"] is not True:
        raise ValueError("Inspection requires retained command")
    actions = {cause["action"] for cause in CONFLICT_RECOVERY["execution_attempt_" + record["operation"]].values()}
    if type(recovery["action"]) is not str or recovery["action"] not in actions:
        raise ValueError("Invalid recovery action")
    _text(recovery["description"], 4096)
    evidence = (recovery["code"], recovery["detail"], recovery["request_id"])
    if any(value is not None for value in evidence):
        causes = CONFLICT_RECOVERY["execution_attempt_" + record["operation"]]
        if type(recovery["code"]) is not str or recovery["code"] not in causes:
            raise ValueError("Invalid recovery cause")
        for name in ("detail", "request_id"):
            if admitted_evidence_text(recovery[name], EVIDENCE[name]) is None:
                raise ValueError("Invalid recovery evidence")
    if recovery["observed_state"] not in (None, "in_progress", "succeeded", "failed", "expired", "not_visible"):
        raise ValueError("Invalid observed state")
    pending = recovery["action"] == "reconcile_state" and recovery["observed_state"] is None
    if pending != (record["phase"] == "terminal_reconciling"):
        raise ValueError("Inconsistent reconciliation phase")
    _fields(restart, "automatic_reads automatic_resends new_work_for_attempt next_actor next_action")
    expected = {"automatic_reads": "retry_with_backoff" if pending else "stopped",
                "automatic_resends": "stopped_including_restart", "new_work_for_attempt": "stopped",
                "next_actor": "provider" if pending else "provider_operator"}
    if any(restart[key] != value for key, value in expected.items()):
        raise ValueError("Inconsistent restart behavior")
    _text(restart["next_action"])
