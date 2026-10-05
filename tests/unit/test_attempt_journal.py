"""Prove durable Attempt records and restart decisions before filesystem effects."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
import unittest

from nmrpeak_provider import attempt_journal
from nmrpeak_provider.attempt_journal import (
    ActiveAttempt,
    LatestDiagnostic,
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
    prepared_terminal_replay,
    retain_terminal_command,
)
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.interpreter import InterpreterUnavailableReason
from nmrpeak_provider.product_input import InputRejectionReason
from nmrpeak_provider.provider_https import ProviderOperation
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
from nmrpeak_provider.runner_protocol import RunnerRejectionReason


ATTEMPT_REF = "execution_attempt:sha256:" + "a" * 64
OTHER_ATTEMPT_REF = "execution_attempt:sha256:" + "b" * 64


class AttemptJournalRecordTests(unittest.TestCase):
    def test_persisted_diagnostic_reason_catalog_is_exhaustive(self) -> None:
        expected = {
            *(reason.value for reason in InputRejectionReason),
            *(reason.value for reason in RunnerRejectionReason),
            *(reason.value for reason in InterpreterUnavailableReason),
            "model_report",
            "runner_candidate_rejected",
        }
        self.assertEqual(attempt_journal._DIAGNOSTIC_REASONS, expected)

    def test_legacy_v1_record_bytes_and_digest_are_unchanged(self) -> None:
        active = active_attempt()
        raw = journal_record_bytes(active)
        self.assertEqual(
            sha256(raw).hexdigest(),
            "75cbff263ac543f6cc82331bef71c6b4dfa32c595ab5dcab87b899734a30ba89",
        )
        self.assertEqual(parse_journal_record(raw), active)
        self.assertEqual(journal_record_bytes(parse_journal_record(raw)), raw)

    def test_v2_diagnostic_survives_execution_and_exact_terminal_replay(self) -> None:
        diagnostic = LatestDiagnostic(
            stage="preparation", kind="candidate_issue", producer="interpreter_candidate",
            reason="unsupported_multiplicity", path="/model_input/spectra/1H/peaks/2/multiplicity",
            endpoint_route=("primary", "fallback"), observed_at="2026-10-04T12:00:00+00:00",
        )
        active = replace(active_attempt(), latest_diagnostic=diagnostic)
        self.assertEqual(json.loads(journal_record_bytes(active))["schema_id"], "nmrpeak.attempt_journal_record.v2")
        self.assertEqual(parse_journal_record(journal_record_bytes(active)), active)
        entered = mark_execution_entered(active)
        command = prepare_execution_attempt_fail(
            execution_attempt_ref=ATTEMPT_REF, failure_code="interpretation_failed",
            failure_message="The interpreted candidate was rejected.",
        )
        terminal = retain_terminal_command(entered, command)
        self.assertEqual(terminal.latest_diagnostic, diagnostic)
        self.assertEqual(terminal.terminal_request_body, command.body)
        self.assertEqual(prepared_terminal_replay(terminal).body, command.body)
        for record in (terminal, replace(terminal, terminal_hold_action="do_not_resend",
                              terminal_hold_description="Investigate before resending.")):
            self.assertEqual(parse_journal_record(journal_record_bytes(record)), record)
            self.assertEqual(record.terminal_request_body, command.body)

    def test_v2_diagnostic_shape_is_closed_and_never_contains_source_text(self) -> None:
        diagnostic = LatestDiagnostic(
            stage="preparation", kind="direct_source_issue", producer="provider",
            reason="unsupported_multiplicity", path="/model_input/spectra/1H/peaks/0/multiplicity",
            observed_at="2026-10-04T12:00:00+00:00",
        )
        document = json.loads(journal_record_bytes(replace(active_attempt(), latest_diagnostic=diagnostic)))
        for change in (
            {"latest_diagnostic": None},
            {"latest_diagnostic": document["latest_diagnostic"] | {"source": "secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"path": "/secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"stage": "private_secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"kind": "private_secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"producer": "private_secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"reason": "secret\nvalue"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"reason": "private_secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"endpoint_route": ["bad endpoint"]}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"observed_at": "2026-10-04T12:00:00"}},
        ):
            with self.subTest(change=change), self.assertRaises((TypeError, ValueError)):
                parse_journal_record(canonical_json_bytes(document | change))
        with self.assertRaises(ValueError):
            parse_journal_record(canonical_json_bytes(document | {"schema_id": "nmrpeak.attempt_journal_record.v1"}))

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

    def test_hold_is_part_of_the_exact_record_identity(self) -> None:
        terminal = retain_terminal_command(active_attempt(), prepare_execution_attempt_fail(
            execution_attempt_ref=ATTEMPT_REF, failure_code="input_rejected",
            failure_message="The input is not supported."))
        held = replace(terminal, terminal_hold_action="do_not_resend",
                       terminal_hold_description="This command cannot be applied.")
        self.assertNotEqual(terminal, held, "Stale callers must not retire a newly held command")
        self.assertEqual(parse_journal_record(journal_record_bytes(held)), held)

    def test_held_record_cannot_downgrade_or_admit_unbounded_evidence(self) -> None:
        terminal = retain_terminal_command(active_attempt(), prepare_execution_attempt_fail(
            execution_attempt_ref=ATTEMPT_REF, failure_code="input_rejected",
            failure_message="The input is not supported."))
        document = json.loads(journal_record_bytes(replace(terminal, terminal_hold_action="do_not_resend",
            terminal_hold_description="This command cannot be applied.")))
        for change in ({"terminal_hold_action": None}, {"terminal_hold_description": None},
                       {"terminal_hold_description": ""}, {"terminal_hold_description": "é" * 2049}):
            with self.subTest(field=next(iter(change))), self.assertRaises((TypeError, ValueError)):
                parse_journal_record(canonical_json_bytes(document | change))
        with self.assertRaises((TypeError, ValueError)):
            replace(terminal, terminal_hold_description="An action is required.")

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

    def test_terminal_replay_wraps_the_retained_body_without_rendering_it_again(self) -> None:
        records = (
            retain_terminal_command(
                active_attempt(),
                prepare_execution_attempt_complete(
                    execution_attempt_ref=ATTEMPT_REF,
                    result_schema_id="nmrpeak.structure_candidates.result.v1",
                    canonical_result=b'{"candidate":"C"}',
                ),
            ),
            retain_terminal_command(
                active_attempt(),
                prepare_execution_attempt_fail(
                    execution_attempt_ref=ATTEMPT_REF,
                    failure_code="input_rejected",
                    failure_message="The input is not supported.",
                ),
            ),
        )
        expected = (
            (
                ProviderOperation.EXECUTION_ATTEMPT_COMPLETE,
                "/provider/v1/execution-attempts/complete",
            ),
            (
                ProviderOperation.EXECUTION_ATTEMPT_FAIL,
                "/provider/v1/execution-attempts/fail",
            ),
        )
        for record, (operation, path) in zip(records, expected, strict=True):
            with self.subTest(operation=operation):
                replay = prepared_terminal_replay(record)
                self.assertIs(operation, replay.operation)
                self.assertEqual("POST", replay.method)
                self.assertEqual(path, replay.path)
                self.assertEqual("", replay.query)
                self.assertEqual(record.terminal_request_body, replay.body)

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

    def test_terminal_restart_retains_reports_until_exact_receipt(self) -> None:
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
            (fail, AttemptState.EXPIRED, RetainTerminalConflict),
            (complete, AttemptState.EXPIRED, RetainTerminalConflict),
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


class TerminalReconciliationJournalTests(unittest.TestCase):
    def test_pending_kind_is_closed_and_canonical_api_evidence_round_trips(self):
        terminal = retain_terminal_command(active_attempt(), prepare_execution_attempt_fail(
            execution_attempt_ref=ATTEMPT_REF, failure_code="input_rejected", failure_message="Input unsupported."))
        pending = replace(terminal, terminal_hold_action="reconcile_state", terminal_hold_description="Reconcile state.",
                          terminal_hold_code="operation_conflict", terminal_hold_detail="A valid\u00a0detail.", terminal_hold_request_id="request:test")
        document = json.loads(journal_record_bytes(pending))
        self.assertEqual(document["record_kind"], "terminal_reconciling")
        self.assertEqual(parse_journal_record(journal_record_bytes(pending)), pending)
        for change in ({"record_kind": "terminal_hold"}, {"terminal_observed_state": "expired"},
                       {"terminal_hold_action": "do_not_resend"}, {"terminal_hold_detail": " leading"},
                       {"terminal_hold_request_id": "bad request"}, {"terminal_hold_code": "unknown_ascii_code"}):
            with self.subTest(change=change), self.assertRaises((TypeError, ValueError)):
                parse_journal_record(canonical_json_bytes(document | change))
