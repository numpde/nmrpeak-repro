"""Prove CHF checkpoint verification without importing or executing Torch."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
from pathlib import Path
import tempfile
import unittest


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "models/nmrpeak_chf_v1/runner/checkpoint_file.py"
)
_SPEC = importlib.util.spec_from_file_location("nmrpeak_chf_checkpoint_file", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
checkpoint_file = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(checkpoint_file)


class ChfCheckpointFileTests(unittest.TestCase):
    def test_verified_descriptor_is_rewound_and_detached_from_path_replacement(self) -> None:
        admitted_bytes = b"admitted checkpoint bytes"
        expected_ref = "sha256:" + sha256(admitted_bytes).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            checkpoint_path = Path(directory) / "checkpoint.pt"
            checkpoint_path.write_bytes(admitted_bytes)

            checkpoint = checkpoint_file._open_verified_checkpoint(
                checkpoint_path,
                expected_ref,
            )
            try:
                checkpoint_path.rename(Path(directory) / "replaced.pt")
                checkpoint_path.write_bytes(b"different bytes at the same path")
                self.assertEqual(checkpoint.tell(), 0)
                self.assertEqual(checkpoint.read(), admitted_bytes)
            finally:
                checkpoint.close()

    def test_rejects_malformed_identity_before_opening_the_path(self) -> None:
        with self.assertRaises(ValueError):
            checkpoint_file._open_verified_checkpoint(
                Path("/path/that/must/not/be-opened"),
                "SHA256:" + "0" * 64,
            )

    def test_rejects_non_regular_or_wrong_checkpoint_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint_path = root / "checkpoint.pt"
            checkpoint_path.write_bytes(b"checkpoint")
            expected_ref = "sha256:" + sha256(b"other").hexdigest()
            with self.assertRaisesRegex(RuntimeError, "differ"):
                checkpoint_file._open_verified_checkpoint(
                    checkpoint_path,
                    expected_ref,
                )

            with self.assertRaisesRegex(RuntimeError, "regular file"):
                checkpoint_file._open_verified_checkpoint(root, expected_ref)

            link_path = root / "checkpoint-link.pt"
            link_path.symlink_to(checkpoint_path)
            with self.assertRaises(OSError):
                checkpoint_file._open_verified_checkpoint(link_path, expected_ref)


if __name__ == "__main__":
    unittest.main()
