"""Prove journal filesystem effects preserve independent durable obligations."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
import unittest
from unittest.mock import patch

from nmrpeak_provider.attempt_journal import (
    ActiveAttempt,
    LocalExecutionPhase,
    StartPending,
    journal_record_bytes,
    journal_record_name,
    retain_terminal_command,
)
from nmrpeak_provider.attempt_journal_store import (
    AttemptJournalConflict,
    AttemptJournalStateRejected,
    AttemptJournalStore,
    AttemptJournalWriteFailed,
)
from nmrpeak_provider.provider_requests import prepare_execution_attempt_complete


ATTEMPT_REF = "execution_attempt:sha256:" + "a" * 64


class AttemptJournalStoreTests(unittest.TestCase):
    def test_admit_replace_reopen_and_retire_one_exact_record(self) -> None:
        with journal_directory() as root:
            start = start_pending("1")
            active = active_attempt(start)
            with AttemptJournalStore(root, maximum_records=2) as store:
                self.assertEqual(store.admit(start), start)
                self.assertEqual(store.records(), (start,))
                self.assertEqual(store.replace(start, active), active)
                self.assertEqual(store.records(), (active,))

            with AttemptJournalStore(root, maximum_records=2) as reopened:
                self.assertEqual(reopened.records(), (active,))
                reopened.retire(active)
                self.assertEqual(reopened.records(), ())

    def test_record_slots_count_every_unresolved_attempt(self) -> None:
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as store:
                store.admit(start_pending("1"))
                with self.assertRaises(AttemptJournalConflict):
                    store.admit(start_pending("2"))
                self.assertEqual(store.records(), (start_pending("1"),))

    def test_two_lanes_cannot_lose_each_others_record(self) -> None:
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as store:
                barrier = Barrier(3)
                failures: list[Exception] = []

                def admit(record: StartPending) -> None:
                    try:
                        barrier.wait()
                        store.admit(record)
                    except Exception as error:
                        failures.append(error)

                threads = [
                    Thread(target=admit, args=(start_pending(digit),))
                    for digit in ("1", "2")
                ]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join()

                self.assertEqual(failures, [])
                self.assertEqual(
                    set(store.records()),
                    {start_pending("1"), start_pending("2")},
                )

    def test_stale_retirement_cannot_delete_a_terminal_obligation(self) -> None:
        with journal_directory() as root:
            start = start_pending("1")
            active = active_attempt(start)
            terminal = retain_terminal_command(
                active,
                prepare_execution_attempt_complete(
                    execution_attempt_ref=ATTEMPT_REF,
                    result_schema_id="nmrpeak.structure_candidates.result.v1",
                    canonical_result=b'{"candidate":"C"}',
                ),
            )
            with AttemptJournalStore(root, maximum_records=1) as store:
                store.admit(start)
                store.replace(start, active)
                store.replace(active, terminal)
                with self.assertRaises(AttemptJournalConflict):
                    store.retire(active)

            with AttemptJournalStore(root, maximum_records=1) as reopened:
                self.assertEqual(reopened.records(), (terminal,))

    def test_recognized_pre_effect_staging_file_is_removed_on_open(self) -> None:
        with journal_directory() as root:
            record = start_pending("1")
            staging = root / f".{journal_record_name(record)}.pending"
            staging.write_bytes(journal_record_bytes(record))
            staging.chmod(0o600)

            with AttemptJournalStore(root, maximum_records=1) as store:
                self.assertEqual(store.records(), ())
            self.assertFalse(staging.exists())

    def test_corrupt_record_blocks_open_before_staging_cleanup(self) -> None:
        with journal_directory() as root:
            corrupt = root / journal_record_name(start_pending("1"))
            corrupt.write_bytes(b"{}")
            corrupt.chmod(0o600)
            staged_record = start_pending("2")
            staging = root / f".{journal_record_name(staged_record)}.pending"
            staging.write_bytes(journal_record_bytes(staged_record))
            staging.chmod(0o600)

            with self.assertRaises(AttemptJournalStateRejected):
                AttemptJournalStore(root, maximum_records=2)
            self.assertTrue(staging.exists())

    def test_root_and_entry_authority_drift_fail_closed(self) -> None:
        with journal_directory() as root:
            root.chmod(0o755)
            with self.assertRaises(AttemptJournalStateRejected):
                AttemptJournalStore(root, maximum_records=1)

        with journal_directory() as root:
            (root / "foreign").write_text("foreign", encoding="utf-8")
            with self.assertRaises(AttemptJournalStateRejected):
                AttemptJournalStore(root, maximum_records=1)

        with journal_directory() as root:
            record = start_pending("1")
            wrong_name = root / ("f" * 64 + ".json")
            wrong_name.write_bytes(journal_record_bytes(record))
            wrong_name.chmod(0o600)
            with self.assertRaises(AttemptJournalStateRejected):
                with AttemptJournalStore(root, maximum_records=1) as store:
                    store.records()

        with journal_directory() as root, TemporaryDirectory() as target_directory:
            target = Path(target_directory) / "target"
            target.write_text("not a record", encoding="utf-8")
            target.chmod(0o600)
            (root / journal_record_name(start_pending("1"))).symlink_to(target)
            with self.assertRaises(AttemptJournalStateRejected):
                with AttemptJournalStore(root, maximum_records=1) as store:
                    store.records()

    def test_failure_before_rename_preserves_the_previous_record(self) -> None:
        with journal_directory() as root:
            start = start_pending("1")
            active = active_attempt(start)
            with AttemptJournalStore(root, maximum_records=1) as store:
                store.admit(start)
                with patch(
                    "nmrpeak_provider.attempt_journal_store.os.fsync",
                    side_effect=OSError("injected file fsync failure"),
                ):
                    with self.assertRaises(AttemptJournalWriteFailed):
                        store.replace(start, active)

            with AttemptJournalStore(root, maximum_records=1) as reopened:
                self.assertEqual(reopened.records(), (start,))

    def test_failure_after_rename_never_claims_a_durable_outcome(self) -> None:
        with journal_directory() as root:
            start = start_pending("1")
            active = active_attempt(start)
            with AttemptJournalStore(root, maximum_records=1) as store:
                store.admit(start)
                with patch(
                    "nmrpeak_provider.attempt_journal_store.os.fsync",
                    side_effect=(None, OSError("injected directory fsync failure")),
                ):
                    with self.assertRaises(AttemptJournalWriteFailed):
                        store.replace(start, active)

            with AttemptJournalStore(root, maximum_records=1) as reopened:
                self.assertEqual(reopened.records(), (active,))

    def test_retirement_fsync_failure_never_reports_success(self) -> None:
        with journal_directory() as root:
            record = start_pending("1")
            with AttemptJournalStore(root, maximum_records=1) as store:
                store.admit(record)
                with patch(
                    "nmrpeak_provider.attempt_journal_store.os.fsync",
                    side_effect=OSError("injected directory fsync failure"),
                ):
                    with self.assertRaises(AttemptJournalWriteFailed):
                        store.retire(record)

            with AttemptJournalStore(root, maximum_records=1) as reopened:
                self.assertEqual(reopened.records(), ())


def start_pending(digit: str) -> StartPending:
    return StartPending(
        job_ref=f"job:test-{digit}",
        provider_attempt_key="nmrpeak-provider.v1:" + digit * 64,
        input_fingerprint="sha256:" + "3" * 64,
        frozen_generation_id="sha256:" + "4" * 64,
    )


def active_attempt(start: StartPending) -> ActiveAttempt:
    return ActiveAttempt(
        job_ref=start.job_ref,
        provider_attempt_key=start.provider_attempt_key,
        input_fingerprint=start.input_fingerprint,
        frozen_generation_id=start.frozen_generation_id,
        execution_attempt_ref=ATTEMPT_REF,
        local_phase=LocalExecutionPhase.PRE_EXECUTION,
    )


@contextmanager
def journal_directory():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        yield root


if __name__ == "__main__":
    unittest.main()
