"""Retained diagnostic history, independent of replay obligations and receipts."""

from datetime import datetime, timezone
import re

from .canonical_json import canonical_json_bytes
from .diagnostic_text import diagnostic_text


MAX_DIAGNOSTIC_BYTES = 262_144
MAX_DIAGNOSTIC_EVENTS = 32
SCHEMA_ID = "nmrpeak.attempt_diagnostics.v1"
_IDENTITIES = {
    "job_ref": r"job:[A-Za-z0-9_.-]{1,124}",
    "provider_attempt_key": r"nmrpeak-provider\.v1:[0-9a-f]{64}",
    "execution_attempt_ref": r"execution_attempt:sha256:[0-9a-f]{64}",
    "frozen_generation_id": r"sha256:[0-9a-f]{64}",
}


def validate_diagnostics(document: object) -> None:
    if type(document) is not dict or set(document) != {"schema_id", "omitted_count", "events", *_IDENTITIES}:
        raise ValueError("Attempt diagnostics have invalid fields")
    if document["schema_id"] != SCHEMA_ID:
        raise ValueError("Attempt diagnostics have an unsupported schema")
    for name, pattern in _IDENTITIES.items():
        if type(document[name]) is not str or re.fullmatch(pattern, document[name]) is None:
            raise ValueError(f"Attempt diagnostics have an invalid {name}")
    if type(document["omitted_count"]) is not int or document["omitted_count"] < 0:
        raise ValueError("Attempt diagnostics have an invalid omitted-event count")
    events = document["events"]
    if type(events) is not list or not 1 <= len(events) <= MAX_DIAGNOSTIC_EVENTS:
        raise ValueError("Attempt diagnostics have an invalid event count")
    for event in events:
        _validate_event(event)
    if len(canonical_json_bytes(document)) > MAX_DIAGNOSTIC_BYTES:
        raise ValueError("Attempt diagnostics exceed their storage limit")


def _validate_event(event: object) -> None:
    if type(event) is not dict or set(event) != {"observed_at", "stage", "message"}:
        raise ValueError("Attempt diagnostic event has invalid fields")
    for name, maximum in (("stage", 128), ("message", 8192), ("observed_at", 64)):
        value = event[name]
        if type(value) is not str or not value or not value.isprintable() or len(value.encode("utf-8")) > maximum:
            raise ValueError(f"Attempt diagnostic event has invalid {name}")
    observed = datetime.fromisoformat(event["observed_at"])
    if observed.utcoffset() != timezone.utc.utcoffset(observed):
        raise ValueError("Attempt diagnostic observation requires UTC")


def require_diagnostic_owner(document: dict, record) -> None:
    if any(document[name] != getattr(record, name) for name in _IDENTITIES):
        raise ValueError("Attempt diagnostic identities changed")


def append_diagnostic(record, previous: dict | None, stage: str, message: str) -> dict:
    identities = {name: getattr(record, name) for name in _IDENTITIES}
    if previous is not None:
        validate_diagnostics(previous)
        require_diagnostic_owner(previous, record)
    event = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "message": diagnostic_text(message, limit=8192),
    }
    _validate_event(event)
    document = {
        "schema_id": SCHEMA_ID, **identities,
        "omitted_count": previous["omitted_count"] if previous else 0,
        "events": [*(previous["events"] if previous else []), event],
    }
    while len(document["events"]) > MAX_DIAGNOSTIC_EVENTS or len(canonical_json_bytes(document)) > MAX_DIAGNOSTIC_BYTES:
        document["events"].pop(0)
        document["omitted_count"] += 1
    validate_diagnostics(document)
    return document
