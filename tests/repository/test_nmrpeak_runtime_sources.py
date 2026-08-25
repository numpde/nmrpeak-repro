"""Prove the additional pinned source inputs used by NMRPeak runners."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from repository_checks.nmrpeak_source import (
    verify_bart_config,
    verify_unicore_source,
)


REPOSITORY_ROOT = Path(__file__).parents[2]


class NmrpeakRuntimeSourceTests(unittest.TestCase):
    def test_unicore_revision_and_live_closure_are_exact(self) -> None:
        verify_unicore_source(REPOSITORY_ROOT)

    def test_wrong_unicore_revision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            declarations = root / "families/nmrpeak"
            declarations.mkdir(parents=True)
            for name in ("unicore-closure.paths", "unicore-closure.sha256"):
                shutil.copy2(
                    REPOSITORY_ROOT / "families/nmrpeak" / name,
                    declarations / name,
                )
            subprocess.run(
                (
                    "git",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    str(REPOSITORY_ROOT / "unicore-upstream"),
                    str(root / "unicore-upstream"),
                ),
                check=True,
            )
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(root / "unicore-upstream"),
                    "checkout",
                    "--quiet",
                    "HEAD^",
                ),
                check=True,
            )

            with self.assertRaisesRegex(ValueError, "declared source revision"):
                verify_unicore_source(root)

    def test_bart_config_bytes_match_the_pinned_public_source(self) -> None:
        verify_bart_config(REPOSITORY_ROOT)

    def test_bart_config_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "families/nmrpeak/bart-base"
            target.mkdir(parents=True)
            for name in ("config.json", "source.json"):
                shutil.copy2(
                    REPOSITORY_ROOT / "families/nmrpeak/bart-base" / name,
                    target / name,
                )
            config = target / "config.json"
            config.write_bytes(config.read_bytes() + b"\n")

            with self.assertRaisesRegex(ValueError, "pinned source"):
                verify_bart_config(root)


if __name__ == "__main__":
    unittest.main()
