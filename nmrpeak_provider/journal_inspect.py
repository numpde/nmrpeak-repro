"""Inspect retained obligations under the deployment's stopped-provider ownership lock."""
from __future__ import annotations

import argparse
from hashlib import sha256
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .attempt_journal import ActiveAttempt, StartPending, TerminalOperation, TerminalPending
from .attempt_journal_store import AttemptJournalStateRejected, AttemptJournalStore
from .canonical_json import canonical_json_bytes, parse_canonical_json_bytes
from .inspection_document import validate_inspection_document
from .attempt_journal_store import JOURNAL_MAXIMUM_RECORDS
from .provider_config import JOURNAL_PATH


def _record_facts(record):
    facts = {"job_ref": record.job_ref, "provider_attempt_key": record.provider_attempt_key,
             "frozen_generation_id": record.frozen_generation_id, "next_actor": "provider_operator",
             "restart_behavior": "Provider startup stays blocked while this record from an earlier provider version remains unresolved."}
    if type(record) is StartPending:
        return facts | {"phase": "start_pending", "delivery": "unconfirmed",
            "next_action": "Preserve this record. Ask the API operator and provider maintainer to reconcile the retained START intent and plan its migration."}
    facts["execution_attempt_ref"] = record.execution_attempt_ref
    if type(record) is ActiveAttempt:
        return facts | {"phase": "active", "local_phase": record.local_phase.value,
            "latest_diagnostic": _diagnostic_facts(record.latest_diagnostic),
            "next_action": "Preserve this record. Ask the API operator and provider maintainer to confirm this Attempt's state and plan migration of the retained obligation."}
    if type(record) is not TerminalPending:
        raise TypeError("Unsupported journal record for inspection")
    facts |= {"operation": record.terminal_operation.value, "delivery": "unconfirmed",
              "command_fingerprint": record.terminal_request_fingerprint,
              "command_byte_count": len(record.terminal_request_body),
              "latest_diagnostic": _diagnostic_facts(record.latest_diagnostic)}
    if record.terminal_operation is TerminalOperation.FAIL:
        command = parse_canonical_json_bytes(record.terminal_request_body)
        facts["retained_failure"] = {
            "failure_code": command["failure_code"], "failure_message": command["failure_message"],
        }
    if record.terminal_hold_action is None:
        return facts | {"phase": "terminal_pending",
            "next_action": "Preserve the exact command and record. Ask the API operator and provider maintainer to reconcile report delivery and plan migration of the retained obligation."}
    if record.terminal_reconciling:
        action = "Preserve the exact command and record. Ask the API operator and provider maintainer to reconcile this Attempt and plan migration."
    else:
        action = "Run make provider/deployment/journal/archive-closed with this record's digest, frozen generation and an investigation reason. The command reads the API to confirm closure before archiving; report delivery remains unconfirmed."
    return facts | {
        "phase": "terminal_reconciling" if record.terminal_reconciling else "terminal_hold",
        "recovery": {
            "job_ref": record.job_ref, "execution_attempt_ref": record.execution_attempt_ref,
            "operation": record.terminal_operation.value, "command_fingerprint": record.terminal_request_fingerprint,
            "command_retained": True, "delivery": "unconfirmed", "action": record.terminal_hold_action,
            "description": record.terminal_hold_description, "code": record.terminal_hold_code,
            "detail": record.terminal_hold_detail, "request_id": record.terminal_hold_request_id,
            "observed_state": record.terminal_observed_state,
        },
        "on_restart": {"automatic_reads": "stopped", "automatic_resends": "stopped_including_restart",
                       "new_work_for_attempt": "stopped", "next_actor": "provider_operator", "next_action": action},
        "next_action": action,
    }


def _diagnostic_facts(diagnostic):
    if diagnostic is None:
        return None
    return {
        "stage": diagnostic.stage,
        "kind": diagnostic.kind,
        "producer": diagnostic.producer,
        "reason": diagnostic.reason,
        "observed_at": diagnostic.observed_at,
        "path": diagnostic.path,
        "endpoint_route": list(diagnostic.endpoint_route),
    }


def inspect_journal(root: Path, execution_attempt_ref: str | None = None) -> bytes:
    """Read validated records without mutation; caller owns stopped deployment exclusion."""
    with AttemptJournalStore(root, maximum_records=JOURNAL_MAXIMUM_RECORDS, read_only=True) as journal:
        records = [_record_facts(record) | {"record_digest": "sha256:" + sha256(journal.record_bytes(record)).hexdigest()}
                   for record in journal.records()
                   if execution_attempt_ref is None or getattr(record, "execution_attempt_ref", None) == execution_attempt_ref]
        diagnostics = journal.diagnostics(execution_attempt_ref)
        omitted = journal.diagnostic_count() - len(diagnostics)
    document = {"schema_id": "nmrpeak.journal_inspection.v4", "api_observation": "not_performed",
        "current_automation": "stopped", "observed_at": datetime.now(UTC).isoformat(),
        "stage_counts": dict(Counter(record["phase"] for record in records)), "records": records,
        "diagnostics": diagnostics, "diagnostics_omitted": omitted}
    validate_inspection_document(document)
    return canonical_json_bytes(document)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect records from an earlier provider version without changing them or sending requests. These records block provider startup until explicitly resolved.")
    parser.add_argument("--execution-attempt-ref", help="Show one Attempt's full retained diagnostic history")
    options = parser.parse_args(arguments)
    try:
        print(inspect_journal(JOURNAL_PATH, options.execution_attempt_ref).decode("utf-8"))
    except (AttemptJournalStateRejected, OSError, ValueError, TypeError) as error:
        parser.exit(2, f"Cannot inspect provider journal: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
