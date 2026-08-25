"""Prove local snapshot classifications retain their filesystem causes."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nmrpeak_provider.local_input import (
    LocalInputFailureReason,
    LocalInputSnapshotError,
    read_bounded_regular_file,
    read_ordered_bounded_regular_files,
)


class LocalInputTests(unittest.TestCase):
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
