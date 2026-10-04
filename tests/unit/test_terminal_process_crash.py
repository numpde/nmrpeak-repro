"""Prove exact terminal recovery after an actual provider process death."""

from __future__ import annotations

from hashlib import sha256
import json
import multiprocessing
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nmrpeak_provider.attempt_journal import TerminalPending
from nmrpeak_provider.attempt_journal_store import AttemptJournalStore
from nmrpeak_provider.attempt_lifecycle import (
    InputFailurePending,
    JobAdmitted,
    StartContinues,
    TerminalDelivered,
    admit_next_job,
    deliver_terminal,
    prepare_execution,
    reconcile_record,
    start_attempt,
)
from nmrpeak_provider.journal_inspect import inspect_journal
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE
from tests.fakes.provider_server import ServerA, serve_server_a
from tests.fakes.tls_certificates import write_test_certificates
from tests.unit.test_attempt_lifecycle_tls import (
    _FROZEN_GENERATION_ID,
    _NoRunnerValidation,
    _api,
    _generation,
    _generation_runtime,
)


_AFTER_RETENTION = 77
_AFTER_RECEIPT = 78


def _crash_at_boundary(mode: str, root_name: str, port: int) -> None:
    """Run in a spawned process so os._exit leaves the journal writer abrupt."""

    root = Path(root_name)
    api = _api(port, root)
    with AttemptJournalStore(root / "journal", maximum_records=1) as journal:
        record, = journal.records()
        if mode == "after_retention":
            original_replace = journal.replace

            def replace_then_exit(original, replacement):
                original_replace(original, replacement)
                if type(replacement) is TerminalPending:
                    os._exit(_AFTER_RETENTION)

            journal.replace = replace_then_exit
            prepare_execution(
                lane=CHF_LIFECYCLE_LANE,
                api=api,
                journal=journal,
                session=_NoRunnerValidation(),
                interpreter=object(),
                record=record,
                canonical_input=b"{}",
            )
        elif mode == "after_receipt":
            if type(record) is not TerminalPending:
                raise AssertionError("The terminal command was not retained")

            def retire_then_exit(_record):
                os._exit(_AFTER_RECEIPT)

            journal.retire = retire_then_exit
            deliver_terminal(api=api, journal=journal, record=record)
        else:
            raise AssertionError("Unknown crash boundary")
    raise AssertionError("The crash boundary was not reached")


class TerminalProcessCrashTests(unittest.TestCase):
    def test_exact_v2_failure_replays_after_both_real_process_crash_windows(self) -> None:
        for mode, expected_exit, expected_replayed in (
            ("after_retention", _AFTER_RETENTION, False),
            ("after_receipt", _AFTER_RECEIPT, True),
        ):
            with self.subTest(mode=mode), TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_test_certificates(root)
                journal_root = root / "journal"
                journal_root.mkdir(mode=0o700)
                state = ServerA(
                    analysis_kind_ref=CHF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                    canonical_input=b"{}",
                )
                generation = _generation(
                    CHF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                    CHF_LIFECYCLE_LANE.offering.implementation_ref,
                )
                with serve_server_a(state=state, certificate_directory=root) as port:
                    api = _api(port, root)
                    with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                        admitted = admit_next_job(
                            lane=CHF_LIFECYCLE_LANE,
                            api=api, journal=journal, generation=generation,
                            frozen_generation_id=_FROZEN_GENERATION_ID,
                        )
                        self.assertIs(type(admitted), JobAdmitted)
                        started = start_attempt(
                            lane=CHF_LIFECYCLE_LANE,
                            api=api, journal=journal, generation=generation,
                            frozen_generation_id=_FROZEN_GENERATION_ID,
                            record=admitted.record,
                        )
                        self.assertIs(type(started), StartContinues)
                        if mode == "after_receipt":
                            prepared = prepare_execution(
                                lane=CHF_LIFECYCLE_LANE,
                                api=api, journal=journal,
                                session=_NoRunnerValidation(),
                                interpreter=object(),
                                record=started.record,
                                canonical_input=admitted.canonical_input,
                            )
                            self.assertIs(type(prepared), InputFailurePending)
                            before_crash = journal.record_bytes(prepared.record)

                    process = multiprocessing.get_context("spawn").Process(
                        target=_crash_at_boundary,
                        args=(mode, str(root), port),
                    )
                    process.start()
                    process.join(timeout=15)
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=5)
                    self.assertEqual(process.exitcode, expected_exit)

                    with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                        retained, = journal.records()
                        self.assertIs(type(retained), TerminalPending)
                        raw = journal.record_bytes(retained)
                        self.assertEqual(
                            json.loads(raw)["schema_id"],
                            "nmrpeak.attempt_journal_record.v2",
                        )
                        if mode == "after_receipt":
                            self.assertEqual(raw, before_crash)
                        terminal_body = retained.terminal_request_body
                        terminal_digest = "sha256:" + sha256(raw).hexdigest()
                        self.assertEqual(retained.latest_diagnostic.reason, "invalid_structure")

                    before_recovery, = json.loads(inspect_journal(journal_root))["records"]
                    self.assertEqual(before_recovery["record_digest"], terminal_digest)
                    self.assertEqual(before_recovery["delivery"], "unconfirmed")
                    self.assertEqual(before_recovery["phase"], "terminal_pending")
                    self.assertEqual(before_recovery["retained_failure"]["failure_code"], "input_rejected")
                    failure_bodies = [
                        body for method, target, body in state.requests
                        if method == "POST" and target == "/provider/v1/execution-attempts/fail"
                    ]
                    self.assertEqual(len(failure_bodies), 0 if mode == "after_retention" else 1)
                    if mode == "after_retention":
                        self.assertEqual(state.attempt.state, "in_progress")
                    else:
                        self.assertEqual(state.attempt.state, "failed")
                        self.assertEqual(failure_bodies[0], terminal_body)

                    with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                        retained_after_reopen, = journal.records()
                        self.assertEqual(journal.record_bytes(retained_after_reopen), raw)
                        recovered = reconcile_record(
                            runtime=_generation_runtime(chf=generation),
                            api=api, journal=journal, record=retained_after_reopen,
                        )
                        self.assertIs(type(recovered), TerminalDelivered, repr(recovered))
                        self.assertIs(recovered.receipt.replayed, expected_replayed)
                        self.assertEqual(journal.records(), ())
                    failure_bodies = [
                        body for method, target, body in state.requests
                        if method == "POST" and target == "/provider/v1/execution-attempts/fail"
                    ]
                    self.assertEqual(failure_bodies, [terminal_body] * (1 if mode == "after_retention" else 2))
                    self.assertEqual(state.attempt.terminal_body, terminal_body)
                    self.assertEqual(state.failures, [])


if __name__ == "__main__":
    unittest.main()
