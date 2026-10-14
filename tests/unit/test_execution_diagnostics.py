import json
import unittest

from nmrpeak_provider.attempt_journal_store import AttemptJournalStore
from nmrpeak_provider.attempt_lifecycle import _log_execution_process_failure, _cancel_session_after_failure, reconcile_record, execute_prepared, PreparedForExecution, ObservationPolicy
from tests.unit.test_attempt_lifecycle import (
    journal_directory, active_attempt, entered_attempt, valid_chf_input, pending_from_active,
    CapturingApi, success_response, attempt_snapshot, generation_runtime, progress_receipt,
)


class ExecutionDiagnosticsTests(unittest.TestCase):
    def test_primary_cleanup_and_restart_explanation_survive(self):
        active = entered_attempt(valid_chf_input())
        primary = RuntimeError("Tensor workspace allocation failed; api_key=actual-secret")

        class Session:
            def cancel(self):
                raise OSError("Runner stop could not reach its supervisor")

        cleanup = _cancel_session_after_failure(Session(), primary, note="Cleanup also failed.")
        self.assertEqual(cleanup, "unconfirmed")
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as store:
                pending = pending_from_active(active)
                store.admit(pending)
                store.replace(pending, active)
                with self.assertLogs("nmrpeak_provider.attempt_lifecycle", level="ERROR"):
                    _log_execution_process_failure(active, journal=store, stage="generation_exchange",
                        failure_kind="generation_failed", error=primary, cleanup_state=cleanup)
            with AttemptJournalStore(root, maximum_records=1) as store:
                message = store.diagnostics(active.execution_attempt_ref)[0]["events"][-1]["message"]
                self.assertIn("allocation failed", message)
                self.assertIn("could not reach its supervisor", message)
                self.assertNotIn("actual-secret", message)
                api = CapturingApi(success_response(attempt_snapshot(
                    execution_attempt_ref=active.execution_attempt_ref,
                    job_ref=active.job_ref, state="in_progress", job_state="open")))
                outcome = reconcile_record(runtime=generation_runtime(), api=api, journal=store, record=active)
                body = outcome.record.terminal_request_body
                public = json.loads(body)["failure_message"]
                self.assertIn("allocation failed", public)
                self.assertIn("uncertain", public)
                self.assertNotIn("actual-secret", public)
                store.record_diagnostic(outcome.record, "delivery", "API reply was lost")
                self.assertEqual(store.records()[0].terminal_request_body, body)

    def test_storage_failure_is_secondary_to_primary_execution_error(self):
        class UnwritableStore:
            def record_diagnostic(self, *args):
                raise OSError("Diagnostic filesystem is full")

        primary = RuntimeError("Original worker failure")
        with self.assertLogs("nmrpeak_provider.attempt_lifecycle", level="ERROR") as logs:
            _log_execution_process_failure(entered_attempt(valid_chf_input()), journal=UnwritableStore(),
                stage="generation_exchange", failure_kind="generation_failed",
                error=primary, cleanup_state="confirmed")
        self.assertIn("Original worker failure", " ".join(logs.output))
        self.assertIn("Diagnostic filesystem is full", " ".join(primary.__notes__))

    def test_unexpected_cleanup_error_does_not_replace_execution_entry_failure(self):
        primary = OSError("Cannot persist execution entry")

        class Store:
            def replace(self, *args):
                raise primary

            def record_diagnostic(self, *args):
                raise OSError("Diagnostic write also failed")

        class Session:
            def cancel(self):
                raise RuntimeError("Unexpected supervisor failure")

        with self.assertLogs("nmrpeak_provider.attempt_lifecycle", level="ERROR"), self.assertRaises(OSError) as raised:
            execute_prepared(api=CapturingApi(success_response(progress_receipt(phase="running"))),
                journal=Store(), session=Session(),
                prepared=PreparedForExecution(active_attempt(valid_chf_input()), object()),
                observation=ObservationPolicy(0.01, 0.1))
        self.assertIs(raised.exception, primary)
        self.assertIn("Unexpected supervisor failure", " ".join(primary.__notes__))
