"""Prove marker-last CHF checkpoint volume admission without Docker."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch


_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "docker/chf_checkpoint_volume.py"
)
_SPEC = importlib.util.spec_from_file_location("chf_checkpoint_volume", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
volume_worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(volume_worker)


class ChfCheckpointVolumeTests(unittest.TestCase):
    def test_population_remeasures_bytes_and_writes_marker_last(self) -> None:
        checkpoint = b"checkpoint fixture"
        marker = b'{"admitted":true}'
        with TemporaryDirectory() as temporary, mounted_volume(Path(temporary)):
            with stdin_bytes(checkpoint):
                volume_worker.populate(
                    len(checkpoint),
                    "sha256:" + sha256(checkpoint).hexdigest(),
                    marker,
                )
            volume_worker.verify(
                len(checkpoint),
                "sha256:" + sha256(checkpoint).hexdigest(),
                marker,
            )
            root = Path(temporary)
            self.assertEqual((root / "checkpoint.pt").read_bytes(), checkpoint)
            self.assertEqual(
                (root / ".nmrpeak-checkpoint.json").read_bytes(), marker
            )
            self.assertEqual((root / "checkpoint.pt").stat().st_mode & 0o777, 0o444)

    def test_bad_stream_never_creates_an_admission_marker(self) -> None:
        with TemporaryDirectory() as temporary, mounted_volume(Path(temporary)):
            with stdin_bytes(b"wrong"), self.assertRaises(
                volume_worker.CheckpointVolumeRejected
            ):
                volume_worker.populate(
                    5,
                    "sha256:" + "0" * 64,
                    b"marker",
                )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_verification_rejects_byte_mode_marker_and_inventory_drift(self) -> None:
        checkpoint = b"checkpoint fixture"
        digest = "sha256:" + sha256(checkpoint).hexdigest()
        marker = b"marker"
        for drift in ("bytes", "mode", "marker", "extra"):
            with self.subTest(drift=drift), TemporaryDirectory() as temporary, mounted_volume(
                Path(temporary)
            ):
                with stdin_bytes(checkpoint):
                    volume_worker.populate(len(checkpoint), digest, marker)
                root = Path(temporary)
                if drift == "bytes":
                    (root / "checkpoint.pt").chmod(0o644)
                    (root / "checkpoint.pt").write_bytes(b"changed")
                    (root / "checkpoint.pt").chmod(0o444)
                elif drift == "mode":
                    (root / "checkpoint.pt").chmod(0o644)
                elif drift == "marker":
                    (root / ".nmrpeak-checkpoint.json").chmod(0o644)
                    (root / ".nmrpeak-checkpoint.json").write_bytes(b"changed")
                    (root / ".nmrpeak-checkpoint.json").chmod(0o444)
                else:
                    (root / "foreign").write_bytes(b"x")
                with self.assertRaises(volume_worker.CheckpointVolumeRejected):
                    volume_worker.verify(len(checkpoint), digest, marker)

    def test_recovery_probe_accepts_only_markerless_owned_shape(self) -> None:
        with TemporaryDirectory() as temporary, mounted_volume(Path(temporary)):
            root = Path(temporary)
            volume_worker.verify_recoverable()
            (root / "checkpoint.pt").write_bytes(b"partial")
            volume_worker.verify_recoverable()
            (root / "foreign").write_bytes(b"x")
            with self.assertRaises(volume_worker.CheckpointVolumeRejected):
                volume_worker.verify_recoverable()
            (root / "foreign").unlink()
            (root / ".nmrpeak-checkpoint.json").write_bytes(b"marker")
            with self.assertRaises(volume_worker.CheckpointVolumeRejected):
                volume_worker.verify_recoverable()


class mounted_volume:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.original: tuple[Path, Path, Path] | None = None

    def __enter__(self) -> None:
        self.original = (
            volume_worker.VOLUME_PATH,
            volume_worker.CHECKPOINT_PATH,
            volume_worker.MARKER_PATH,
        )
        volume_worker.VOLUME_PATH = self.root
        volume_worker.CHECKPOINT_PATH = self.root / "checkpoint.pt"
        volume_worker.MARKER_PATH = self.root / ".nmrpeak-checkpoint.json"

    def __exit__(self, *_error: object) -> None:
        assert self.original is not None
        (
            volume_worker.VOLUME_PATH,
            volume_worker.CHECKPOINT_PATH,
            volume_worker.MARKER_PATH,
        ) = self.original


class stdin_bytes:
    def __init__(self, raw: bytes) -> None:
        self.stream = types.SimpleNamespace(buffer=io.BytesIO(raw))
        self.patch = patch.object(volume_worker.sys, "stdin", self.stream)

    def __enter__(self) -> None:
        self.patch.start()

    def __exit__(self, *_error: object) -> None:
        self.patch.stop()


if __name__ == "__main__":
    unittest.main()
