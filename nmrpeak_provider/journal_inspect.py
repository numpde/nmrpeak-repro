"""Inspect retained obligations under the deployment's stopped-provider ownership lock."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .attempt_journal import ActiveAttempt, LocalExecutionPhase, StartPending, TerminalPending
from .attempt_journal_store import AttemptJournalStateRejected, AttemptJournalStore
from .attempt_lifecycle import terminal_recovery_facts
from .canonical_json import canonical_json_bytes
from .inspection_document import validate_inspection_document
from .provider_config import JOURNAL_MAXIMUM_RECORDS, JOURNAL_PATH


def _record_facts(record):
    facts = {"job_ref": record.job_ref, "provider_attempt_key": record.provider_attempt_key,
             "frozen_generation_id": record.frozen_generation_id, "next_actor": "provider_operator"}
    if type(record) is StartPending:
        return facts | {"phase": "start_pending", "delivery": "unconfirmed",
            "restart_behavior": "Reuse the retained start intent after validating the owning frozen generation.",
            "next_action": "Restore the owning deployment configuration and credentials before restarting."}
    facts["execution_attempt_ref"] = record.execution_attempt_ref
    if type(record) is ActiveAttempt:
        behavior = ("Read current Attempt state and resume only the retained pre-execution phase."
                    if record.local_phase is LocalExecutionPhase.PRE_EXECUTION else
                    "Read current Attempt state; report interrupted execution if still active, without rerunning analysis.")
        return facts | {"phase": "active", "local_phase": record.local_phase.value,
                        "restart_behavior": behavior,
                        "next_action": "Preserve this record and restart the owning deployment to reconcile its Attempt."}
    if type(record) is not TerminalPending:
        raise TypeError("Unsupported journal record for inspection")
    facts |= {"operation": record.terminal_operation.value, "delivery": "unconfirmed",
              "command_fingerprint": record.terminal_request_fingerprint,
              "command_byte_count": len(record.terminal_request_body)}
    if record.terminal_hold_action is None:
        return facts | {"phase": "terminal_pending",
            "restart_behavior": "Read current Attempt state and recover the exact retained command; do not recompute it.",
            "next_action": "Restore service access and restart the owning deployment; preserve the exact command."}
    recovery = terminal_recovery_facts(record)
    return facts | {"phase": "terminal_reconciling" if record.terminal_reconciling else "terminal_hold",
        "recovery": {key: value for key, value in recovery.items()
                     if key not in {"automatic_reads", "automatic_resends", "new_work_for_attempt", "next_actor", "next_action"}},
        "on_restart": {key: recovery[key] for key in
                       ("automatic_reads", "automatic_resends", "new_work_for_attempt", "next_actor", "next_action")},
        "restart_behavior": ("Retry only the Attempt read with backoff; publication remains paused."
                             if record.terminal_reconciling else "Keep publication and reads stopped; retain the command for operator reconciliation."),
        "next_action": ("Restore API read access, then restart the owning deployment."
                        if record.terminal_reconciling else recovery["next_action"])}


def inspect_journal(root: Path) -> bytes:
    """Read validated records without mutation; caller owns stopped deployment exclusion."""
    with AttemptJournalStore(root, maximum_records=JOURNAL_MAXIMUM_RECORDS, read_only=True) as journal:
        records = [_record_facts(record) for record in journal.records()]
    document = {"schema_id": "nmrpeak.journal_inspection.v1",
        "current_automation": "stopped", "observed_at": datetime.now(UTC).isoformat(),
        "stage_counts": dict(Counter(record["phase"] for record in records)), "records": records}
    validate_inspection_document(document)
    return canonical_json_bytes(document)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect a stopped provider's retained obligations without sending requests.")
    parser.parse_args(arguments)
    try:
        print(inspect_journal(JOURNAL_PATH).decode("utf-8"))
    except (AttemptJournalStateRejected, OSError, ValueError, TypeError) as error:
        parser.exit(2, f"Cannot inspect provider journal: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
