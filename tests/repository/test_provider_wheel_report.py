"""Verify the target-interpreter provider wheel report boundary."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from repository_checks.provider_wheel_report import LOCKS


class ProviderWheelReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "source"
        self.pool = root / "pool"
        (self.source / "dependencies").mkdir(parents=True)
        self.pool.mkdir()
        wheel = self.pool / "example_pkg-1.0-py3-none-any.whl"
        with ZipFile(wheel, "w") as archive:
            archive.writestr("example_pkg/__init__.py", "")
            archive.writestr("example_pkg-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: example-pkg\nVersion: 1.0\n")
            archive.writestr("example_pkg-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr("example_pkg-1.0.dist-info/RECORD", "")
        digest = sha256(wheel.read_bytes()).hexdigest()
        (self.source / LOCKS[0]).write_text("-c dependencies/provider-core-source.txt\n-r dependencies/provider-core-api.lock\n")
        (self.source / LOCKS[1]).write_text(f"example-pkg==1.0 --hash=sha256:{digest}\n")
        (self.source / LOCKS[2]).write_text(
            "example-pkg @ https://github.com/numpde/nmr-provider-tools/releases/download/v1/"
            f"{wheel.name} --hash=sha256:{digest}\n"
        )
        self.digests = [sha256((self.source / name).read_bytes()).hexdigest() for name in LOCKS]
        self.wheel = wheel

    def run_report(self):
        script = Path(__file__).resolve().parents[2] / "repository_checks/provider_wheel_report.py"
        args = [sys.executable, "-I", str(script), "--source", str(self.source), "--pool", str(self.pool)]
        for digest in self.digests:
            args.extend(("--lock-sha256", digest))
        return subprocess.run(args, capture_output=True, check=False)

    def test_report_selects_the_hash_locked_local_wheel(self):
        result = self.run_report()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        selected = json.loads(result.stdout)["install"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["download_info"]["archive_info"]["hashes"]["sha256"],
                         sha256(self.wheel.read_bytes()).hexdigest())

    def test_changed_lock_or_wheel_never_emits_a_report(self):
        (self.source / LOCKS[1]).write_text("changed")
        result = self.run_report()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"lock changed", result.stderr)
        (self.source / LOCKS[1]).write_text(
            f"example-pkg==1.0 --hash=sha256:{sha256(self.wheel.read_bytes()).hexdigest()}\n"
        )
        self.wheel.write_bytes(b"changed")
        result = self.run_report()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

if __name__ == "__main__":
    unittest.main()
