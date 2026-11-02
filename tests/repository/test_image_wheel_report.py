"""Verify target-interpreter wheel reports for provider and runner images."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from repository_checks.image_wheel_report import LOCKS as PROFILES

LOCKS = PROFILES["provider"]


class ImageWheelReportTests(unittest.TestCase):
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

    def run_report(self, profile="provider", digests=None):
        script = Path(__file__).resolve().parents[2] / "repository_checks/image_wheel_report.py"
        args = [sys.executable, "-I", str(script), "--source", str(self.source), "--pool", str(self.pool),
                "--profile", profile]
        for digest in self.digests if digests is None else digests:
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

    def test_native_and_scientific_profiles_select_local_wheels(self):
        (self.source / PROFILES["native"][0]).write_text(
            "-c dependencies/provider-core-source.txt\n-r dependencies/provider-core-native.lock\n"
        )
        (self.source / PROFILES["native"][1]).write_bytes((self.source / LOCKS[1]).read_bytes())
        scientific = self.source / PROFILES["scientific"][0]
        scientific.parent.mkdir(parents=True)
        scientific.write_text(
            f"example-pkg @ https://download.pytorch.org/whl/cpu/{self.wheel.name} "
            f"--hash=sha256:{sha256(self.wheel.read_bytes()).hexdigest()}\n"
        )
        for profile in ("native", "scientific"):
            with self.subTest(profile=profile):
                digests = [sha256((self.source / name).read_bytes()).hexdigest() for name in PROFILES[profile]]
                result = self.run_report(profile, digests)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(len(json.loads(result.stdout)["install"]), 1)

    def test_scientific_profile_rejects_missing_wheel(self):
        scientific = self.source / PROFILES["scientific"][0]
        scientific.parent.mkdir(parents=True)
        scientific.write_text(
            f"example-pkg @ https://download.pytorch.org/whl/cpu/{self.wheel.name} "
            f"--hash=sha256:{sha256(self.wheel.read_bytes()).hexdigest()}\n"
        )
        digest = sha256(scientific.read_bytes()).hexdigest()
        self.wheel.unlink()
        result = self.run_report("scientific", [digest])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"No such file", result.stderr)

    def test_scientific_direct_url_decodes_cpu_version_filename(self):
        wheel = self.pool / "torch-2.3.0+cpu-py3-none-any.whl"
        with ZipFile(wheel, "w") as archive:
            archive.writestr("torch/__init__.py", "")
            archive.writestr("torch-2.3.0+cpu.dist-info/METADATA", "Metadata-Version: 2.1\nName: torch\nVersion: 2.3.0+cpu\n")
            archive.writestr("torch-2.3.0+cpu.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr("torch-2.3.0+cpu.dist-info/RECORD", "")
        scientific = self.source / PROFILES["scientific"][0]
        scientific.parent.mkdir(parents=True)
        digest = sha256(wheel.read_bytes()).hexdigest()
        scientific.write_text(
            "torch @ https://download.pytorch.org/whl/cpu/torch-2.3.0%2Bcpu-py3-none-any.whl"
            f"#sha256={digest} --hash=sha256:{digest}\n"
        )
        result = self.run_report("scientific", [sha256(scientific.read_bytes()).hexdigest()])
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("torch-2.3.0%2Bcpu", json.loads(result.stdout)["install"][0]["download_info"]["url"])

if __name__ == "__main__":
    unittest.main()
