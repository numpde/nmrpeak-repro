"""Prove durable Attempt records and restart decisions before filesystem effects."""

from __future__ import annotations

import json
import unittest

from nmrpeak_provider.attempt_journal import (
    ActiveAttempt,
    LocalExecutionPhase,
    ObserveUntilExpiry,
    PublishInterruptedFailure,
    ReplayStart,
    ReplayTerminal,
    ResumePreExecution,
    RetainTerminalConflict,
    RetireResolved,
    StartPending,
    TerminalOperation,
    TerminalPending,
    bind_started_attempt,
    decide_restart,
    journal_record_bytes,
    journal_record_name,
    mark_execution_entered,
    parse_journal_record,
    retain_terminal_command,
)
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.provider_requests import (
    prepare_execution_attempt_complete,
    prepare_execution_attempt_fail,
)
from nmrpeak_provider.provider_success import (
    AttemptState,
    ExecutionAttemptSnapshot,
    ExecutionAttemptStarted,
    JobState,
)


ATTEMPT_REF = "execution_attempt:sha256:" + "a" * 64
OTHER_ATTEMPT_REF = "execution_attempt:sha256:" + "b" * 64


class AttemptJournalRecordTests(unittest.TestCase):
    def test_every_record_variant_has_one_canonical_round_trip(self) -> None:
        start = start_pending()
        active = active_attempt()
        entered = mark_execution_entered(active)
        terminal = retain_terminal_command(
            entered,
            prepare_execution_attempt_complete(
                execution_attempt_ref=ATTEMPT_REF,
                result_schema_id="nmrpeak.structure_candidates.result.v1",
                canonical_result=b'{"candidate":"C"}',
            ),
        )
        for record in (start, active, entered, terminal):
            with self.subTest(record_type=type(record).__name__):
                raw = journal_record_bytes(record)
                self.assertEqual(parse_journal_record(raw), record)
                self.assertNotIn(terminal.terminal_request_body, repr(record).encode())
        self.assertEqual("1" * 64 + ".json", journal_record_name(start))

    def test_record_loader_rejects_shape_version_and_identity_drift(self) -> None:
        document = json.loads(journal_record_bytes(start_pending()))
        cases = (
            document | {"extra": True},
            document | {"schema_id": "nmrpeak.attempt_journal_record.v2"},
            document | {"provider_attempt_key": "foreign-key"},
            document | {"input_fingerprint": "sha256:short"},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    parse_journal_record(canonical_json_bytes(changed))
        with self.assertRaises(ValueError):
            parse_journal_record(journal_record_bytes(start_pending()) + b"\n")

    def test_terminal_loader_binds_digest_operation_and_attempt(self) -> None:
        terminal = retain_terminal_command(
            active_attempt(),
            prepare_execution_attempt_fail(
                execution_attempt_ref=ATTEMPT_REF,
                failure_code="input_rejected",
                failure_message="The input is not supported.",
            ),
        )
        document = json.loads(journal_record_bytes(terminal))
        cases = (
            document | {"terminal_request_fingerprint": "sha256:" + "0" * 64},
            document | {"terminal_operation": "complete"},
            document | {"execution_attempt_ref": OTHER_ATTEMPT_REF},
            document | {"terminal_request_base64": "not+canonical"},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    parse_journal_record(canonical_json_bytes(changed))

    def test_transitions_bind_receipt_and_terminal_command_once(self) -> None:
        start = start_pending()
        active = bind_started_attempt(start, started_receipt())
        self.assertEqual(active, active_attempt())
        entered = mark_execution_entered(active)
        self.assertEqual(entered.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
        with self.assertRaises(ValueError):
            mark_execution_entered(entered)

        terminal = retain_terminal_command(
            entered,
            prepare_execution_attempt_fail(
                execution_attempt_ref=ATTEMPT_REF,
                failure_code="input_rejected",
                failure_message="The input is not supported.",
            ),
        )
        self.assertEqual(terminal.terminal_operation, TerminalOperation.FAIL)
        with self.assertRaises(ValueError):
            retain_terminal_command(
                active,
                prepare_execution_attempt_fail(
                    execution_attempt_ref=OTHER_ATTEMPT_REF,
                    failure_code="input_rejected",
                    failure_message="The input is not supported.",
                ),
            )

    def test_start_binding_rejects_job_drift_and_terminal_receipts(self) -> None:
        with self.assertRaises(ValueError):
            bind_started_attempt(
                start_pending(),
                started_receipt(job_ref="job:other"),
            )
        with self.assertRaises(ValueError):
            bind_started_attempt(
                start_pending(),
                started_receipt(state=AttemptState.SUCCEEDED, replayed=True),
            )


class AttemptRestartDecisionTests(unittest.TestCase):
    def test_pending_start_replays_without_inventing_an_attempt(self) -> None:
        record = start_pending()
        self.assertEqual(decide_restart(record, None), ReplayStart(record))
        with self.assertRaises(ValueError):
            decide_restart(record, snapshot())

    def test_active_attempt_restart_table_is_closed(self) -> None:
        pre_execution = active_attempt()
        execution_entered = mark_execution_entered(pre_execution)
        cases = (
            (
                pre_execution,
                snapshot(),
                ResumePreExecution,
            ),
            (
                execution_entered,
                snapshot(),
                PublishInterruptedFailure,
            ),
        )
        for phase_record in (pre_execution, execution_entered):
            for job_state in (JobState.CLOSED, JobState.CANCELLED):
                cases += (
                    (
                        phase_record,
                        snapshot(job_state=job_state),
                        ObserveUntilExpiry,
                    ),
                )
            for state in (
                AttemptState.SUCCEEDED,
                AttemptState.FAILED,
                AttemptState.EXPIRED,
            ):
                cases += (
                    (
                        phase_record,
                        snapshot(state=state),
                        RetireResolved,
                    ),
                )
        for record, server_snapshot, expected_type in cases:
            with self.subTest(
                phase=record.local_phase,
                attempt_state=server_snapshot.state,
                job_state=server_snapshot.job_state,
            ):
                decision = decide_restart(record, server_snapshot)
                self.assertIs(type(decision), expected_type)
        interrupted = decide_restart(execution_entered, snapshot())
        self.assertEqual(
            interrupted.failure_code,
            "provider_execution_interrupted",
        )

    def test_terminal_restart_requires_receipt_or_authoritative_expiry(self) -> None:
        complete = retain_terminal_command(
            active_attempt(),
            prepare_execution_attempt_complete(
                execution_attempt_ref=ATTEMPT_REF,
                result_schema_id="nmrpeak.structure_candidates.result.v1",
                canonical_result=b'{"candidate":"C"}',
            ),
        )
        fail = retain_terminal_command(
            active_attempt(),
            prepare_execution_attempt_fail(
                execution_attempt_ref=ATTEMPT_REF,
                failure_code="input_rejected",
                failure_message="The input is not supported.",
            ),
        )
        cases = (
            (complete, AttemptState.IN_PROGRESS, ReplayTerminal),
            (complete, AttemptState.SUCCEEDED, ReplayTerminal),
            (complete, AttemptState.FAILED, RetainTerminalConflict),
            (fail, AttemptState.FAILED, ReplayTerminal),
            (fail, AttemptState.SUCCEEDED, RetainTerminalConflict),
            (fail, AttemptState.EXPIRED, RetireResolved),
        )
        for record, state, expected_type in cases:
            with self.subTest(operation=record.terminal_operation, state=state):
                self.assertIs(
                    type(decide_restart(record, snapshot(state=state))),
                    expected_type,
                )

    def test_restart_rejects_a_snapshot_for_another_record(self) -> None:
        with self.assertRaises(ValueError):
            decide_restart(
                active_attempt(),
                snapshot(execution_attempt_ref=OTHER_ATTEMPT_REF),
            )


def start_pending() -> StartPending:
    return StartPending(
        job_ref="job:test",
        provider_attempt_key="nmrpeak-provider.v1:" + "1" * 64,
        input_fingerprint="sha256:" + "2" * 64,
        frozen_generation_id="sha256:" + "3" * 64,
    )


def active_attempt() -> ActiveAttempt:
    return ActiveAttempt(
        job_ref="job:test",
        provider_attempt_key="nmrpeak-provider.v1:" + "1" * 64,
        input_fingerprint="sha256:" + "2" * 64,
        frozen_generation_id="sha256:" + "3" * 64,
        execution_attempt_ref=ATTEMPT_REF,
        local_phase=LocalExecutionPhase.PRE_EXECUTION,
    )


def started_receipt(
    *,
    job_ref: str = "job:test",
    state: AttemptState = AttemptState.IN_PROGRESS,
    replayed: bool = False,
) -> ExecutionAttemptStarted:
    return ExecutionAttemptStarted(
        execution_attempt_ref=ATTEMPT_REF,
        job_ref=job_ref,
        analysis_kind_ref="mol_from_1h_peaks",
        provider_ref="provider:test",
        state=state,
        started_at="2026-08-24T12:00:00Z",
        replayed=replayed,
    )


def snapshot(
    *,
    execution_attempt_ref: str = ATTEMPT_REF,
    state: AttemptState = AttemptState.IN_PROGRESS,
    job_state: JobState = JobState.OPEN,
) -> ExecutionAttemptSnapshot:
    return ExecutionAttemptSnapshot(
        execution_attempt_ref=execution_attempt_ref,
        job_ref="job:test",
        state=state,
        job_state=job_state,
    )


if __name__ == "__main__":
    unittest.main()
