"""Prove journal inventory projects only complete frozen-generation references."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nmrpeak_provider.attempt_journal import (
    StartPending,
    journal_record_bytes,
    journal_record_name,
)
from nmrpeak_provider.attempt_journal_store import AttemptJournalStore
from nmrpeak_provider.canonical_json import parse_canonical_json_bytes
from nmrpeak_provider.journal_inventory import journal_generation_inventory


class JournalInventoryTests(unittest.TestCase):
    def test_inventory_reports_each_referenced_generation_once(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            with AttemptJournalStore(root, maximum_records=3) as journal:
                journal.admit(record("1", "4"))
                journal.admit(record("2", "4"))
                journal.admit(record("3", "5"))

            document = parse_canonical_json_bytes(
                journal_generation_inventory(root)
            )

        self.assertEqual(
            document,
            {
                "schema_id": "nmrpeak.journal_generation_inventory.v1",
                "frozen_generation_ids": [
                    "sha256:" + "4" * 64,
                    "sha256:" + "5" * 64,
                ],
            },
        )

    def test_inventory_leaves_valid_pre_effect_staging_untouched(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            pending = record("1", "4")
            staging = root / f".{journal_record_name(pending)}.pending"
            staging.write_bytes(journal_record_bytes(pending))
            staging.chmod(0o600)

            document = parse_canonical_json_bytes(
                journal_generation_inventory(root)
            )

            self.assertTrue(staging.is_file())
            self.assertEqual(document["frozen_generation_ids"], [])


def record(attempt_digit: str, generation_digit: str) -> StartPending:
    return StartPending(
        job_ref=f"job:test-{attempt_digit}",
        provider_attempt_key="nmrpeak-provider.v1:" + attempt_digit * 64,
        input_fingerprint="sha256:" + "3" * 64,
        frozen_generation_id="sha256:" + generation_digit * 64,
    )


if __name__ == "__main__":
    unittest.main()
