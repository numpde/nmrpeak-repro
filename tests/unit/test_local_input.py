"""Prove local snapshot classifications retain their filesystem causes."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nmrpeak_provider.local_input import (
    LocalInputFailureReason,
    LocalInputSnapshotError,
    read_bounded_regular_file,
    read_ordered_bounded_regular_files,
)


class LocalInputTests(unittest.TestCase):
    def test_exact_limit_and_partial_reads_produce_the_complete_file(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(b"abcdef")
            read = os.read
            with patch("nmrpeak_provider.local_input.os.read", side_effect=lambda fd, size: read(fd, min(size, 2))):
                self.assertEqual(read_bounded_regular_file(path, maximum_bytes=6), b"abcdef")

    def test_oversized_file_is_refused_before_reading(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(b"abcdef")
            with patch("nmrpeak_provider.local_input.os.read", side_effect=AssertionError("oversized input read")):
                with self.assertRaises(LocalInputSnapshotError) as raised:
                    read_bounded_regular_file(path, maximum_bytes=5)
        self.assertIs(raised.exception.reason, LocalInputFailureReason.TOO_LARGE)

    def test_read_failure_and_independent_close_failure_both_survive(self) -> None:
        original = OSError("read failed")
        cleanup = OSError("close failed")
        close = os.close
        closed = []

        def close_then_fail(descriptor):
            close(descriptor)
            closed.append(descriptor)
            raise cleanup

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(b"input")
            with (
                patch("nmrpeak_provider.local_input.os.read", side_effect=original),
                patch("nmrpeak_provider.local_input.os.close", side_effect=close_then_fail),
                self.assertRaises(ExceptionGroup) as raised,
            ):
                read_bounded_regular_file(path, maximum_bytes=10)
        self.assertEqual(len(closed), 1)
        self.assertEqual(len(raised.exception.exceptions), 2)
        primary, retirement = raised.exception.exceptions
        self.assertIs(primary.__cause__, original)
        self.assertIs(retirement.__cause__, cleanup)

    def test_process_interruption_survives_close_as_its_exact_object(self) -> None:
        original = SystemExit(7)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(b"input")
            with patch("nmrpeak_provider.local_input.os.read", side_effect=original):
                with self.assertRaises(SystemExit) as raised:
                    read_bounded_regular_file(path, maximum_bytes=10)
        self.assertIs(raised.exception, original)

    def test_fifo_is_refused_without_waiting_for_a_writer(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            os.mkfifo(path)
            with self.assertRaises(LocalInputSnapshotError) as raised:
                read_bounded_regular_file(path, maximum_bytes=10)
        self.assertIs(raised.exception.reason, LocalInputFailureReason.NOT_REGULAR)

    def test_size_admitted_at_open_must_match_the_read_bytes(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(b"original")
            fstat = os.fstat

            def truncate_after_inspection(descriptor):
                facts = fstat(descriptor)
                path.write_bytes(b"new")
                return facts

            with patch("nmrpeak_provider.local_input.os.fstat", side_effect=truncate_after_inspection):
                with self.assertRaises(LocalInputSnapshotError) as raised:
                    read_bounded_regular_file(path, maximum_bytes=100)
        self.assertIs(raised.exception.reason, LocalInputFailureReason.UNREADABLE)

    def test_missing_file_retains_the_open_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing"
            with self.assertRaises(LocalInputSnapshotError) as raised:
                read_bounded_regular_file(path, maximum_bytes=10)

        self.assertIs(raised.exception.reason, LocalInputFailureReason.UNREADABLE)
        self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)

    def test_invalid_selected_file_retains_the_snapshot_cause_chain(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "target"
            target.write_bytes(b"endpoint")
            os.symlink(target, directory / "endpoint.toml")
            with self.assertRaises(LocalInputSnapshotError) as raised:
                read_ordered_bounded_regular_files(
                    directory,
                    filename_suffix=".toml",
                    maximum_directory_entries=2,
                    maximum_files=1,
                    maximum_file_bytes=100,
                )

        self.assertIs(
            raised.exception.reason,
            LocalInputFailureReason.INVALID_SELECTED_FILE,
        )
        self.assertIsInstance(raised.exception.__cause__, OSError)


if __name__ == "__main__":
    unittest.main()
