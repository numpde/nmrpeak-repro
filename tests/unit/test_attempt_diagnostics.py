import json
from unittest.mock import patch
import unittest

from nmrpeak_provider.attempt_diagnostic import MAX_DIAGNOSTIC_BYTES
from nmrpeak_provider.attempt_journal import MAX_JOURNAL_RECORD_BYTES
from nmrpeak_provider.attempt_journal_store import AttemptJournalStore, AttemptJournalStateRejected, AttemptJournalWriteFailed
from nmrpeak_provider.journal_inspect import inspect_journal
from tests.unit.test_attempt_journal_store import journal_directory, start_pending, active_attempt, filesystem_with_available_bytes


class AttemptDiagnosticTests(unittest.TestCase):
    def _admit(self, store):
        start = start_pending("1")
        active = active_attempt(start)
        store.admit(start)
        store.replace(start, active)
        return active

    def test_diagnostics_survive_retirement_and_inspection_without_replay(self):
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as store:
                active = self._admit(store)
                before = store.record_bytes(active)
                store.record_diagnostic(active, "preparation", "Unsupported multiplicity xy; api_key=secret-value")
                self.assertEqual(store.record_bytes(active), before)
                store.retire(active)
            document = json.loads(inspect_journal(root))
            self.assertEqual(document["records"], [])
            evidence, = document["diagnostics"]
            self.assertIn("multiplicity xy", evidence["events"][0]["message"])
            self.assertNotIn("secret-value", json.dumps(document))
            with AttemptJournalStore(root, maximum_records=1) as store:
                self.assertEqual(store.records(), ())
                store.admit(start_pending("2"))

    def test_history_bounds_are_visible(self):
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as store:
                active = self._admit(store)
                for index in range(35):
                    store.record_diagnostic(active, "generation", f"Failure {index}: " + "x" * 8192)
                evidence, = store.diagnostics()
                self.assertGreaterEqual(evidence["omitted_count"], 3)
                self.assertEqual(len(evidence["events"]) + evidence["omitted_count"], 35)
                self.assertIn("Failure 34", evidence["events"][-1]["message"])
                self.assertLessEqual(next(root.glob("*.diagnostic.json")).stat().st_size, MAX_DIAGNOSTIC_BYTES)

    def test_invalid_evidence_fails_admission(self):
        for mutation in ("mode", "json", "identity", "owner", "symlink"):
            with self.subTest(mutation=mutation), journal_directory() as root:
                with AttemptJournalStore(root, maximum_records=1) as store:
                    active = self._admit(store)
                    store.record_diagnostic(active, "generation", "Cannot allocate tensor")
                path, = root.glob("*.diagnostic.json")
                if mutation == "mode":
                    path.chmod(0o644)
                elif mutation == "json":
                    path.write_text("{}")
                elif mutation == "identity":
                    path.rename(root / ("b" * 64 + ".diagnostic.json"))
                elif mutation == "owner":
                    document = json.loads(path.read_bytes())
                    document["job_ref"] = "job:another"
                    from nmrpeak_provider.canonical_json import canonical_json_bytes
                    path.write_bytes(canonical_json_bytes(document))
                else:
                    path.unlink()
                    path.symlink_to("/dev/null")
                with self.assertRaises(AttemptJournalStateRejected):
                    AttemptJournalStore(root, maximum_records=1)

    def test_evidence_write_failure_keeps_original_obligation(self):
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as store:
                active = self._admit(store)
                before = store.record_bytes(active)
                with patch("nmrpeak_provider.attempt_journal_store.os.fsync", side_effect=OSError("Disk full")):
                    with self.assertRaises(AttemptJournalWriteFailed):
                        store.record_diagnostic(active, "generation", "Primary failure")
            with AttemptJournalStore(root, maximum_records=1) as store:
                self.assertEqual(store.record_bytes(active), before)

    def test_evidence_cannot_consume_obligation_recovery_reserve(self):
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as store:
                active = self._admit(store)
                reserve = 2 * MAX_JOURNAL_RECORD_BYTES - len(store.record_bytes(active))
                with patch("nmrpeak_provider.attempt_journal_store.os.fstatvfs", return_value=filesystem_with_available_bytes(reserve)):
                    with self.assertRaises(AttemptJournalStateRejected):
                        store.record_diagnostic(active, "generation", "Failure")
                self.assertEqual(store.records(), (active,))

    def test_inspection_selects_history_and_reports_omissions(self):
        from dataclasses import replace
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as store:
                for index in range(6):
                    start = start_pending(str(index + 1))
                    active = replace(active_attempt(start), execution_attempt_ref="execution_attempt:sha256:" + str(index) * 64)
                    store.admit(start)
                    store.replace(start, active)
                    for _ in range(32):
                        store.record_diagnostic(active, "generation", f"Attempt {index} failure " + "x" * 7900)
                    store.retire(active)
            default = json.loads(inspect_journal(root))
            self.assertEqual(len(default["diagnostics"]), 1)
            self.assertEqual(default["diagnostics_omitted"], 5)
            self.assertLess(len(json.dumps(default).encode()), 1_048_576)
            selected = json.loads(inspect_journal(root, active.execution_attempt_ref))
            self.assertIn("Attempt 5 failure", selected["diagnostics"][0]["events"][0]["message"])
