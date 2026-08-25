"""Prove provider volume admission owns only lock and journal roots."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[2]
HELPER = ROOT / "docker/provider_volume.py"


class ProviderVolumeTests(unittest.TestCase):
    def test_identity_lock_is_created_once_and_exactly_reverified(self) -> None:
        module = load_helper()
        with TemporaryDirectory() as temporary:
            volume = Path(temporary)
            lock = volume / "provider.lock"
            real_fstat = os.fstat

            def root_owned_status(descriptor: int) -> os.stat_result:
                values = list(real_fstat(descriptor))
                values[stat.ST_UID] = 0
                values[stat.ST_GID] = 0
                return os.stat_result(values)

            with (
                volume_paths(module, volume),
                patch.object(module.os, "fstat", side_effect=root_owned_status),
            ):
                module.admit_identity_lock("provider:nmrpeak")
                module.admit_identity_lock("provider:nmrpeak")
                self.assertEqual(lock.read_bytes(), b"provider:nmrpeak\n")
                self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o444)
                with self.assertRaisesRegex(
                    module.ProviderVolumeRejected,
                    "has drifted",
                ):
                    module.admit_identity_lock("provider:other")

    def test_journal_admission_leaves_record_semantics_to_the_provider(self) -> None:
        module = load_helper()
        with TemporaryDirectory() as temporary:
            volume = Path(temporary)
            journal = volume / "journal"
            with (
                volume_paths(module, volume),
                patch.object(module.os, "chown"),
                patch.object(
                    module.Path,
                    "lstat",
                    autospec=True,
                    side_effect=lambda path: journal_status(path, journal),
                ),
            ):
                module.admit_journal()
                (journal / ("1" * 64 + ".json")).write_bytes(b"provider-owned")
                module.admit_journal()
                self.assertEqual(
                    {entry.name for entry in journal.iterdir()},
                    {"1" * 64 + ".json"},
                )
                (volume / "foreign").write_bytes(b"")
                with self.assertRaisesRegex(
                    module.ProviderVolumeRejected,
                    "inventory is invalid",
                ):
                    module.admit_journal()


def load_helper():
    spec = importlib.util.spec_from_file_location("provider_volume", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def volume_paths(module, volume: Path):
    return patch.multiple(
        module,
        VOLUME_PATH=volume,
        IDENTITY_LOCK_PATH=volume / "provider.lock",
        JOURNAL_PATH=volume / "journal",
    )


def journal_status(path: Path, journal: Path) -> os.stat_result:
    status = os.lstat(path)
    if path != journal:
        return status
    values = list(status)
    values[stat.ST_UID] = 65532
    values[stat.ST_GID] = 65532
    return os.stat_result(values)


if __name__ == "__main__":
    unittest.main()
