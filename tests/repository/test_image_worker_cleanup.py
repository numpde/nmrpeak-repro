"""Exercise worker retirement through a fake selected engine."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ImageWorkerRetirementTests(unittest.TestCase):
    def test_joined_stream_drops_source_inputs_but_keeps_receipts_and_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "context").mkdir()
            (root / "context/source").write_text("source")
            for name in ("base.id", "image.id", "proxy.log", "proxy.port", "selection.json"):
                (root / name).write_text("data")
            subprocess.run(["bash", "-ec", 'source "$1"; trim_streamed_image_capture "$2"',
                            "bash", str(ROOT / "scripts/image-operation-lock.sh"), directory],
                           check=True, timeout=5)
            self.assertEqual({path.name for path in root.iterdir()},
                             {"base.id", "image.id", "proxy.log", "proxy.port"})

    def run_worker(self, state, *, removal_status=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text('''#!/usr/bin/env bash
case "$3" in
  inspect) printf '%s\\n' "$FAKE_STATE" ;;
  rm) printf 'removed\\n' > "$FAKE_REMOVAL"; exit "$FAKE_RM_STATUS" ;;
  *) exit 2 ;;
esac
''')
            docker.chmod(0o755)
            receipt = root / "worker.cid"
            receipt.write_text("a" * 64 + "\n")
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       NMRPEAK_DOCKER_HOST="unix:///engine.sock", FAKE_STATE=state,
                       FAKE_REMOVAL=str(root / "removed"), FAKE_RM_STATUS=str(removal_status))
            result = subprocess.run(["bash", "-ec", 'source "$1"; retire_image_worker "$2" expected; printf "exit=%s\\n" "$image_worker_exit_code"',
                                     "bash", str(ROOT / "scripts/image-operation-lock.sh"), str(receipt)],
                                    env=env, capture_output=True, text=True, timeout=5)
            return result, (root / "removed").exists()

    def test_confirmed_success_and_failure_are_retired(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                result, removed = self.run_worker(f"false {exit_code} expected")
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, f"exit={exit_code}\n")
                self.assertTrue(removed)

    def test_running_or_foreign_worker_is_preserved(self):
        for state in ("true 0 expected", "false 0 foreign"):
            with self.subTest(state=state):
                result, removed = self.run_worker(state)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(removed)

    def test_failed_removal_is_not_reported_as_retired(self):
        result, removed = self.run_worker("false 0 expected", removal_status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(removed)


if __name__ == "__main__":
    unittest.main()
