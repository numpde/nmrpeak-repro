"""Prove one provider boot owns one exact local readiness marker."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nmrpeak_provider.provider_readiness import ProviderReadiness, is_provider_ready


class ProviderReadinessTests(unittest.TestCase):
    def test_stale_marker_is_cleared_then_this_boot_publishes_and_retires(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "ready"
            path.write_bytes(b"ready\n")
            path.chmod(0o444)

            readiness = ProviderReadiness.begin(path)
            self.assertFalse(path.exists())
            self.assertFalse(is_provider_ready(path))
            readiness.publish()
            self.assertTrue(is_provider_ready(path))
            readiness.close()
            self.assertFalse(path.exists())

    def test_suspicious_stale_path_is_never_replaced(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_text("outside", encoding="ascii")
            path = root / "ready"
            path.symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "not an owned"):
                ProviderReadiness.begin(path)
            self.assertTrue(path.is_symlink())
            self.assertFalse(is_provider_ready(path))

    def test_cleanup_refuses_to_unlink_a_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "ready"
            readiness = ProviderReadiness.begin(path)
            readiness.publish()
            path.unlink()
            path.write_bytes(b"ready\n")
            path.chmod(0o444)

            with self.assertRaisesRegex(RuntimeError, "changed before cleanup"):
                readiness.close()
            self.assertTrue(path.exists())

    def test_failure_after_publish_retains_enough_identity_for_cleanup(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "ready"
            readiness = ProviderReadiness.begin(path)
            real_fsync = os.fsync
            calls = 0

            def fail_directory_fsync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory fsync failed")
                real_fsync(descriptor)

            with (
                patch(
                    "nmrpeak_provider.provider_readiness.os.fsync",
                    side_effect=fail_directory_fsync,
                ),
                self.assertRaisesRegex(OSError, "directory fsync failed"),
            ):
                readiness.publish()
            self.assertTrue(path.exists())
            readiness.close()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
