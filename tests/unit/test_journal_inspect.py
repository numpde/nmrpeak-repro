"""Operator inspection must explain retained work without acquiring execution authority."""
from dataclasses import replace
from pathlib import Path
import json
from hashlib import sha256
import subprocess
import sys
import unittest
from unittest.mock import patch

from nmrpeak_provider.attempt_journal import LatestDiagnostic, journal_record_bytes, journal_record_name
from nmrpeak_provider.journal_inspect import inspect_journal
from tests.unit.test_attempt_lifecycle import journal_directory, terminal_pending, TerminalOperation
from tests.unit.test_attempt_journal import start_pending, active_attempt


class JournalInspectionTests(unittest.TestCase):
    def _write(self, root, record):
        path = root / journal_record_name(record)
        path.write_bytes(journal_record_bytes(record))
        path.chmod(0o600)
        return path

    def test_every_phase_explains_stopped_inspection_and_restart_without_payload(self):
        terminal = terminal_pending(TerminalOperation.FAIL)
        cases = ((start_pending(), "start_pending"), (active_attempt(), "active"),
                 (terminal, "terminal_pending"),
                 (replace(terminal, terminal_hold_action="reconcile_state",
                          terminal_hold_description="Reconcile state and durable replay facts."), "terminal_reconciling"),
                 (replace(terminal, terminal_hold_action="do_not_resend", terminal_observed_state="expired",
                          terminal_hold_description="Do not resend this command."), "terminal_hold"))
        for record, phase in cases:
            with self.subTest(phase=phase), journal_directory() as root:
                path = self._write(root, record)
                before = path.read_bytes()
                with patch("nmrpeak_provider.provider_api.ProviderApiClient.send", side_effect=AssertionError("Inspection cannot send")):
                    raw = inspect_journal(root)
                doc = json.loads(raw)
                self.assertEqual(doc["schema_id"], "nmrpeak.journal_inspection.v2")
                self.assertEqual(doc["current_automation"], "stopped")
                self.assertEqual(doc["stage_counts"], {phase: 1})
                item, = doc["records"]
                self.assertEqual(item["record_digest"], "sha256:" + sha256(before).hexdigest())
                self.assertEqual(item["phase"], phase)
                self.assertEqual(item["job_ref"], record.job_ref)
                self.assertEqual(item["provider_attempt_key"], record.provider_attempt_key)
                self.assertTrue(item["restart_behavior"])
                self.assertTrue(item["next_action"])
                self.assertEqual(path.read_bytes(), before)
                if hasattr(record, "terminal_request_body"):
                    self.assertEqual(item["command_fingerprint"], record.terminal_request_fingerprint)
                    self.assertEqual(item["command_byte_count"], len(record.terminal_request_body))
                    self.assertEqual(item["delivery"], "unconfirmed")
                    self.assertNotIn("terminal_request_body", raw.decode())
                    self.assertEqual(item["latest_diagnostic"], None)
                    self.assertEqual(
                        item["retained_failure"]["failure_message"],
                        json.loads(record.terminal_request_body)["failure_message"],
                    )
                    if record.terminal_hold_action:
                        self.assertEqual(item["recovery"]["observed_state"], record.terminal_observed_state)

    def test_v2_inspection_shows_bounded_diagnostic_without_source_document(self):
        diagnostic = LatestDiagnostic(
            stage="preparation", kind="direct_source_issue", producer="provider",
            reason="unsupported_multiplicity", path="/model_input/spectra/1H/peaks/0/multiplicity",
            endpoint_route=(), observed_at="2026-10-04T12:00:00+00:00",
        )
        with journal_directory() as root:
            self._write(root, replace(active_attempt(), latest_diagnostic=diagnostic))
            rendered = inspect_journal(root)
        record, = json.loads(rendered)["records"]
        self.assertEqual(record["latest_diagnostic"]["reason"], "unsupported_multiplicity")
        self.assertEqual(record["latest_diagnostic"]["path"], diagnostic.path)
        self.assertNotIn("observed_scalar", rendered.decode())

    def test_v1_inspection_document_remains_accepted_by_new_reader(self):
        from nmrpeak_provider.inspection_document import validate_inspection_document
        with journal_directory() as root:
            self._write(root, terminal_pending(TerminalOperation.FAIL))
            document = json.loads(inspect_journal(root))
        document["schema_id"] = "nmrpeak.journal_inspection.v1"
        record, = document["records"]
        del record["latest_diagnostic"]
        del record["retained_failure"]
        validate_inspection_document(document)

    def test_v2_complete_inspection_omits_failure_text(self):
        with journal_directory() as root:
            self._write(root, terminal_pending(TerminalOperation.COMPLETE))
            rendered = inspect_journal(root)
        record, = json.loads(rendered)["records"]
        self.assertEqual(record["operation"], "complete")
        self.assertNotIn("retained_failure", record)
        self.assertNotIn("canonical_result_base64", rendered.decode())

    def test_missing_and_malformed_journals_are_rejected_without_creation(self):
        with journal_directory() as root:
            missing = root / "missing"
            with self.assertRaises(Exception):
                inspect_journal(missing)
            self.assertFalse(missing.exists())
            path = self._write(root, start_pending())
            path.write_bytes(b"not a journal")
            with self.assertRaises(Exception):
                inspect_journal(root)
            self.assertEqual(path.read_bytes(), b"not a journal")

    def test_module_entry_point_returns_canonical_document_without_model_imports(self):
        with journal_directory() as root:
            self._write(root, start_pending())
            script = """import runpy, sys
from pathlib import Path
import nmrpeak_provider.provider_config as config
config.JOURNAL_PATH = Path(sys.argv[1])
sys.argv = ["journal_inspect"]
try:
    runpy.run_module("nmrpeak_provider.journal_inspect", run_name="__main__")
except SystemExit as error:
    assert error.code == 0
assert not any(name in sys.modules for name in ("torch", "transformers", "unicore"))
"""
            result = subprocess.run([sys.executable, "-c", script, str(root)], capture_output=True, check=True, timeout=10)
            self.assertEqual(json.loads(result.stdout)["stage_counts"], {"start_pending": 1})
            self.assertEqual(result.stderr, b"")


class InspectionDocumentTests(unittest.TestCase):
    def test_closed_document_rejects_malformed_records_and_payload_fields(self):
        from nmrpeak_provider.inspection_document import validate_inspection_document
        with journal_directory() as root:
            JournalInspectionTests()._write(root, terminal_pending(TerminalOperation.FAIL))
            valid = json.loads(inspect_journal(root))
        validate_inspection_document(valid)
        import copy
        cases = []
        for key, value in (("records", [None]), ("body_base64", "private"),
                           ("stage_counts", {"terminal_pending": True})):
            bad = copy.deepcopy(valid)
            bad[key] = value
            cases.append(bad)
        for key, value in (("body_base64", "private"), ("command_byte_count", True),
                           ("phase", "unknown"), ("next_action", "x" * 8193),
                           ("latest_diagnostic", {"source": "secret"}),
                           ("retained_failure", {"failure_code": "bad", "failure_message": "secret", "raw_input": "secret"})):
            bad = copy.deepcopy(valid)
            bad["records"][0][key] = value
            cases.append(bad)
        for bad in cases:
            with self.subTest(document=bad), self.assertRaises(ValueError):
                validate_inspection_document(bad)

    def test_nested_recovery_must_agree_with_record_and_restart_phase(self):
        from nmrpeak_provider.inspection_document import validate_inspection_document
        import copy
        terminal = replace(terminal_pending(TerminalOperation.FAIL),
                           terminal_hold_action="reconcile_state",
                           terminal_hold_description="Read current state.")
        with journal_directory() as root:
            JournalInspectionTests()._write(root, terminal)
            valid = json.loads(inspect_journal(root))
        for section, key, value in (
            ("recovery", "job_ref", "job:other"),
            ("recovery", "command_retained", False),
            ("recovery", "observed_state", "expired"),
            ("recovery", "body_base64", "private"),
            ("recovery", "detail", "unpaired evidence"),
            ("on_restart", "automatic_resends", "retry"),
            ("on_restart", "next_actor", "end_user"),
        ):
            bad = copy.deepcopy(valid)
            bad["records"][0][section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                validate_inspection_document(bad)
