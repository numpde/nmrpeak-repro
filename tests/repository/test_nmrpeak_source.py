"""Prove the NMRPeak build closure rejects unreviewed source bytes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from repository_checks.nmrpeak_source import (
    verify_materialized_nmrpeak_source,
    verify_nmrpeak_source,
)


REPOSITORY_ROOT = Path(__file__).parents[2]


@contextmanager
def copied_source_closure() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        declaration_directory = root / "families/nmrpeak"
        declaration_directory.mkdir(parents=True)
        for name in ("source-closure.paths", "source-closure.sha256"):
            shutil.copy2(
                REPOSITORY_ROOT / "families/nmrpeak" / name,
                declaration_directory / name,
            )
        subprocess.run(
            (
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                str(REPOSITORY_ROOT / "nmrpeak-upstream"),
                str(root / "nmrpeak-upstream"),
            ),
            check=True,
        )
        yield root


class NmrpeakSourceTests(unittest.TestCase):
    def test_declared_committed_and_live_source_are_identical(self) -> None:
        verify_nmrpeak_source(REPOSITORY_ROOT)

    def test_modified_live_source_is_rejected(self) -> None:
        with copied_source_closure() as root:
            source = root / "nmrpeak-upstream/nmrpeak/infer.py"
            source.write_bytes(source.read_bytes() + b"\n")

            with self.assertRaisesRegex(ValueError, "live source content drift"):
                verify_nmrpeak_source(root)

    def test_untracked_file_inside_closure_is_rejected(self) -> None:
        with copied_source_closure() as root:
            unexpected = root / "nmrpeak-upstream/nmrpeak/unreviewed.py"
            unexpected.write_text("pass\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "live source inventory"):
                verify_nmrpeak_source(root)

    def test_materialized_build_source_rejects_an_extra_file(self) -> None:
        with copied_source_closure() as root:
            unexpected = root / "nmrpeak-upstream/dict/unreviewed.txt"
            unexpected.write_text("unreviewed\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "live source inventory"):
                verify_materialized_nmrpeak_source(
                    root / "nmrpeak-upstream",
                    root / "families/nmrpeak/source-closure.paths",
                    root / "families/nmrpeak/source-closure.sha256",
                )

    def test_symlinked_source_file_is_rejected(self) -> None:
        with copied_source_closure() as root:
            source = root / "nmrpeak-upstream/nmrpeak/infer.py"
            source.unlink()
            source.symlink_to("__init__.py")

            with self.assertRaisesRegex(ValueError, "symlink"):
                verify_nmrpeak_source(root)

    def test_absolute_declaration_root_is_rejected(self) -> None:
        with copied_source_closure() as root:
            declaration = root / "families/nmrpeak/source-closure.paths"
            raw = declaration.read_text(encoding="ascii")
            declaration.write_text(raw + "/tmp\n", encoding="ascii")

            with self.assertRaisesRegex(ValueError, "invalid root"):
                verify_nmrpeak_source(root)

    def test_special_source_file_is_rejected(self) -> None:
        with copied_source_closure() as root:
            source = root / "nmrpeak-upstream/nmrpeak/infer.py"
            source.unlink()
            os.mkfifo(source)

            with self.assertRaisesRegex(ValueError, "special file"):
                verify_nmrpeak_source(root)

    def test_wrong_submodule_revision_is_rejected(self) -> None:
        with copied_source_closure() as root:
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(root / "nmrpeak-upstream"),
                    "checkout",
                    "--quiet",
                    "HEAD^",
                ),
                check=True,
            )

            with self.assertRaisesRegex(ValueError, "declared source revision"):
                verify_nmrpeak_source(root)

    def test_manifest_content_drift_is_rejected(self) -> None:
        with copied_source_closure() as root:
            manifest = root / "families/nmrpeak/source-closure.sha256"
            raw = manifest.read_bytes()
            manifest.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])

            with self.assertRaisesRegex(ValueError, "committed source content drift"):
                verify_nmrpeak_source(root)


if __name__ == "__main__":
    unittest.main()
