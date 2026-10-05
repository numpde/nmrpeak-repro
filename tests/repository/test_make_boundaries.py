"""Prove operator supplied Make values remain data at shell boundaries."""

from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).parents[2]


class MakeBoundaryTests(unittest.TestCase):
    def _assert_dry_run_keeps_values_out_of_shell(self, target: str, values: tuple[str, ...]) -> None:
        marker = "/tmp/nmrpeak-make-injection-marker"
        Path(marker).unlink(missing_ok=True)
        injected = f'payload"; touch {marker}; echo "'
        result = subprocess.run(
            ("make", "--dry-run", target, *(f"{name}={injected}" for name in values)),
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(f"touch {marker}", result.stdout)
        self.assertFalse(Path(marker).exists())

    def test_persistent_job_confirmation_cannot_inject_shell_syntax(self) -> None:
        marker = "/tmp/nmrpeak-make-injection-marker"
        Path(marker).unlink(missing_ok=True)
        common = (
            "LIVE_API_ORIGIN=https://example.invalid",
            "LIVE_API_TOPOLOGY=topology",
            "LIVE_USER_CREDENTIAL=/tmp/credential",
            "LIVE_PROJECT_REF=project",
            "LIVE_PROVIDER_REF=provider",
            "LIVE_RUN_LABEL=run",
            "LIVE_STATE=/tmp/state",
            "LIVE_SOURCE_REVISION=" + "0" * 40,
            f'CONFIRM_PERSISTENT_JOBS=1"; touch {marker}; echo "',
        )
        for target, additional in (
            ("test/live/failure-propagation", ()),
            (
                "test/live/success-propagation",
                (
                    "LIVE_EXPECTED_HF_CHECKPOINT=hf",
                    "LIVE_EXPECTED_CHF_CHECKPOINT=chf",
                    "LIVE_EXPECTED_HF_IMAGE_INPUT_ID=" + "sha256:" + "1" * 64,
                    "LIVE_EXPECTED_CHF_IMAGE_INPUT_ID=" + "sha256:" + "2" * 64,
                ),
            ),
        ):
            with self.subTest(target=target):
                result = subprocess.run(
                    ("make", "--dry-run", target, *common, *additional),
                    cwd=REPOSITORY_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(f"touch {marker}", result.stdout)
                self.assertFalse(Path(marker).exists())

    def test_operator_target_values_cannot_inject_shell_syntax(self) -> None:
        cases = (
            ("runner/lock/stage", ("TARGET",)),
            ("runner/lock/check", ("TARGET",)),
            ("runner/lock/apply", ("TARGET",)),
            ("runner/image/build", ("RUNNER", "TARGET")),
            ("checkpoint/import", ("RUNNER", "RELEASE", "ARCHIVE")),
            ("checkpoint/recover", ("VOLUME", "CONFIRM")),
        )
        for target, values in cases:
            with self.subTest(target=target):
                self._assert_dry_run_keeps_values_out_of_shell(target, values)

    def test_image_builder_rejects_a_non_wireless_interface(self) -> None:
        result = subprocess.run(
            (str(REPOSITORY_ROOT / "scripts/test-image-base.sh"), str(REPOSITORY_ROOT), "lo"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a kernel wireless interface", result.stderr)


if __name__ == "__main__":
    unittest.main()
