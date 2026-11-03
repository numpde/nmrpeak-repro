"""Check that image work excludes retirement for the same checkout."""

import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ImageOperationLockTests(unittest.TestCase):
    def test_concurrent_first_builds_create_private_captures(self):
        with tempfile.TemporaryDirectory() as directory:
            command = ["bash", "-c", 'source "$1"; image_capture_directory "$2"',
                       "bash", str(ROOT / "scripts/image-operation-lock.sh"), directory]
            builds = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                      for _ in range(2)]
            outputs = [build.communicate(timeout=5) for build in builds]
            self.assertEqual([build.returncode for build in builds], [0, 0], outputs)
            paths = [Path(output.strip()) for output, _ in outputs]
            self.assertEqual(len(set(paths)), 2)
            self.assertTrue(all(path.is_dir() for path in paths))

    def test_update_stops_after_lock_or_build_failure(self):
        for failure in ("lock", "build"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "scripts").mkdir()
                shutil.copyfile(ROOT / "Makefile", root / "Makefile")
                scripts = {
                    "docker-context.sh": 'printf "unix:///tmp/unused.sock\\n"\n',
                    "test-image-key.sh": 'printf "%064d\\n" 0\n',
                    "image-operation-lock.sh": "hold_image_operation_lock() { return " + ("2" if failure == "lock" else "0") + "; }\n",
                    "provider-image.sh": "exit " + ("8" if failure == "build" else "0") + "\n",
                    "python": f'printf called > "{root / "updated"}"\n',
                }
                for name, content in scripts.items():
                    path = root / "scripts" / name
                    path.write_text("#!/usr/bin/env bash\n" + content)
                    path.chmod(0o755)
                result = subprocess.run(
                    ["make", "provider/deployment/update", "DEPLOYMENT=test", "DOCKER_CONTEXT=science",
                     f"PYTHON={root / 'scripts/python'}"], cwd=root, capture_output=True, text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "updated").exists())

    def test_active_image_work_blocks_exclusive_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                ["bash", "-c", 'source "$1"; hold_image_operation_lock "$2"; printf "ready\\n"; read -r',
                 "bash", str(ROOT / "scripts/image-operation-lock.sh"), directory],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(process.stdout.readline(), "ready\n")
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    process.stdin.write("done\n")
                    process.stdin.flush()
                    self.assertEqual(process.wait(timeout=5), 0)
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
