from dataclasses import replace
import json
import unittest
from nmrpeak_provider.attempt_journal import LocalExecutionPhase, TerminalOperation, journal_record_bytes, parse_journal_record, retain_terminal_command
from nmrpeak_provider.attempt_journal_store import AttemptJournalStore
from nmrpeak_provider.attempt_lifecycle import deliver_terminal, TerminalDelivered, TerminalPublicationHeld, TerminalReconciliationPending
from nmrpeak_provider.provider_https import ProviderRequestUnavailable, RequestDelivery, ProviderOperation
from nmrpeak_provider.provider_outcomes import AttemptMutationCommitPossible
from nmrpeak_provider.provider_requests import prepare_execution_attempt_fail
from tests.unit.test_attempt_lifecycle import (CapturingApi, active_attempt, valid_chf_input, terminal_pending, journal_directory, persist_terminal, success_response, progress_receipt, terminal_receipt, attempt_snapshot)
from tests.unit.test_current_api_failure_semantics import problem_response


class TerminalConditionTests(unittest.TestCase):
    def test_phase_provenance_survives_retention_and_legacy_round_trip(self):
        active = active_attempt(valid_chf_input())
        terminal = retain_terminal_command(active, prepare_execution_attempt_fail(execution_attempt_ref=active.execution_attempt_ref, failure_code="input_rejected", failure_message="Input preparation failed."))
        self.assertIs(terminal.local_phase, LocalExecutionPhase.PRE_EXECUTION)
        self.assertEqual(parse_journal_record(journal_record_bytes(terminal)), terminal)
        document = json.loads(journal_record_bytes(terminal))
        del document["local_phase"]
        from nmrpeak_provider.canonical_json import canonical_json_bytes
        legacy = parse_journal_record(canonical_json_bytes(document))
        self.assertIsNone(legacy.local_phase)
        self.assertEqual(journal_record_bytes(legacy), canonical_json_bytes(document))

    def test_disposable_progress_preserves_terminal_and_terminal_retry_priority(self):
        for phase, operation in ((LocalExecutionPhase.PRE_EXECUTION, TerminalOperation.FAIL), (LocalExecutionPhase.EXECUTION_ENTERED, TerminalOperation.COMPLETE)):
            with self.subTest(phase=phase), journal_directory() as root:
                terminal = replace(terminal_pending(operation), local_phase=phase)
                progress = progress_receipt(phase="preparing" if phase is LocalExecutionPhase.PRE_EXECUTION else "running")
                progress["condition_code"] = "terminal_report_delivery_retryable"
                api = CapturingApi(ProviderRequestUnavailable(RequestDelivery.POSSIBLE), success_response(progress), success_response(terminal_receipt(terminal, replayed=True)))
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    persist_terminal(journal, terminal)
                    outcome = deliver_terminal(api=api, journal=journal, record=terminal)
                    self.assertIsInstance(outcome, AttemptMutationCommitPossible)
                    self.assertEqual(journal.records(), (terminal,))
                    self.assertEqual(json.loads(api.requests[1].body)["phase"], progress["phase"])
                    self.assertIsInstance(deliver_terminal(api=api, journal=journal, record=terminal), TerminalDelivered)
                    self.assertEqual(journal.records(), ())
                self.assertEqual([r.operation for r in api.requests], [api.requests[0].operation, ProviderOperation.EXECUTION_ATTEMPT_PROGRESS, api.requests[0].operation])
                self.assertEqual(api.requests[0].body, api.requests[2].body)

    def test_progress_failure_or_closed_race_does_not_change_terminal_uncertainty(self):
        for response in (ProviderRequestUnavailable(RequestDelivery.POSSIBLE), success_response({"schema_id": "wrong"}),
                         problem_response(409, "operation-conflict", "Operation conflict", "execution_attempt_progress_terminal", "The Attempt is already terminal.")):
            with self.subTest(response=response), journal_directory() as root:
                terminal = terminal_pending(TerminalOperation.COMPLETE)
                api = CapturingApi(ProviderRequestUnavailable(RequestDelivery.POSSIBLE), response)
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    persist_terminal(journal, terminal)
                    outcome = deliver_terminal(api=api, journal=journal, record=terminal)
                    self.assertIsInstance(outcome, AttemptMutationCommitPossible)
                    self.assertEqual(journal.records(), (terminal,))
                self.assertEqual(len(api.requests), 2)

    def test_held_recovery_only_publishes_latest_held_condition(self):
        terminal = replace(terminal_pending(TerminalOperation.FAIL), terminal_hold_action="reconcile_original", terminal_hold_description="Investigate the original command.")
        progress = progress_receipt(phase="running")
        progress["condition_code"] = "terminal_report_delivery_held"
        api = CapturingApi(success_response(progress))
        with journal_directory() as root, AttemptJournalStore(
            root, maximum_records=1
        ) as journal, self.assertLogs(
            "nmrpeak_provider.attempt_lifecycle", level="INFO"
        ) as logs:
            persist_terminal(journal, terminal)
            outcome = deliver_terminal(api=api, journal=journal, record=terminal)
            self.assertIsInstance(outcome, TerminalPublicationHeld)
            self.assertEqual(journal.records(), (terminal,))
        self.assertEqual([r.operation for r in api.requests], [ProviderOperation.EXECUTION_ATTEMPT_PROGRESS])
        self.assertEqual(json.loads(api.requests[0].body)["condition_code"], progress["condition_code"])
        rendered = "\n".join(logs.output)
        self.assertIn("provider_event='terminal_recovery_held'", rendered)
        self.assertIn("action='reconcile_original'", rendered)
        self.assertIn("automatic_reads='stopped'", rendered)
        self.assertIn("provider_event='attempt_condition_confirmed'", rendered)

    def test_legacy_unknown_phase_keeps_exact_command_without_guessing(self):
        terminal = replace(terminal_pending(TerminalOperation.FAIL), local_phase=None)
        api = CapturingApi(ProviderRequestUnavailable(RequestDelivery.POSSIBLE))
        with journal_directory() as root, AttemptJournalStore(root, maximum_records=1) as journal, self.assertLogs("nmrpeak_provider.attempt_lifecycle", level="WARNING") as logs:
            persist_terminal(journal, terminal)
            deliver_terminal(api=api, journal=journal, record=terminal)
            self.assertEqual(journal.records(), (terminal,))
        self.assertEqual(len(api.requests), 1)
        self.assertIn("phase provenance is unavailable", " ".join(logs.output))

    def test_read_reconciliation_condition_is_superseded_by_actual_hold(self):
        terminal = replace(terminal_pending(TerminalOperation.COMPLETE), terminal_hold_action="reconcile_state", terminal_hold_description="Read the current Attempt state.")
        reconciling = progress_receipt(phase="running") | {"condition_code": "terminal_report_delivery_reconciling"}
        held = progress_receipt(phase="running") | {"condition_code": "terminal_report_delivery_held"}
        api = CapturingApi(ProviderRequestUnavailable(RequestDelivery.POSSIBLE), success_response(reconciling),
                           success_response(attempt_snapshot(execution_attempt_ref=terminal.execution_attempt_ref,
                                            job_ref=terminal.job_ref, state="in_progress", job_state="open")), success_response(held))
        with journal_directory() as root, self.assertLogs(
            "nmrpeak_provider.attempt_lifecycle", level="INFO"
        ) as logs:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                persist_terminal(journal, terminal)
                self.assertIsInstance(deliver_terminal(api=api, journal=journal, record=terminal), TerminalReconciliationPending)
            with AttemptJournalStore(root, maximum_records=1) as journal:
                self.assertIsInstance(deliver_terminal(api=api, journal=journal, record=journal.records()[0]), TerminalPublicationHeld)
                self.assertEqual(journal.records()[0].terminal_request_body, terminal.terminal_request_body)
        self.assertEqual([r.operation for r in api.requests], [ProviderOperation.EXECUTION_ATTEMPT_READ, ProviderOperation.EXECUTION_ATTEMPT_PROGRESS] * 2)
        self.assertEqual([json.loads(api.requests[i].body)["condition_code"] for i in (1, 3)], [reconciling["condition_code"], held["condition_code"]])
        rendered = "\n".join(logs.output)
        self.assertIn("automatic_reads='retry_with_backoff'", rendered)
        self.assertIn("automatic_reads='stopped'", rendered)
        self.assertIn("observed_state='in_progress'", rendered)

    def test_known_closed_attempt_does_not_receive_obsolete_condition(self):
        terminal = replace(terminal_pending(TerminalOperation.FAIL), terminal_hold_action="do_not_resend",
                           terminal_hold_description="The Attempt expired.", terminal_observed_state="expired")
        api = CapturingApi()
        with journal_directory() as root, AttemptJournalStore(root, maximum_records=1) as journal:
            persist_terminal(journal, terminal)
            self.assertIsInstance(deliver_terminal(api=api, journal=journal, record=terminal), TerminalPublicationHeld)
            self.assertEqual(journal.records(), (terminal,))
        self.assertEqual(api.requests, [])
