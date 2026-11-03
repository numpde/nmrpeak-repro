"""Qualify the installed SDK boundary used by the contained provider runtime."""

from importlib import import_module
from importlib.metadata import distribution
from pathlib import Path
import re
import subprocess
import sys
import unittest


class ProviderCoreInstallationTests(unittest.TestCase):
    def test_supported_python_imports_the_released_sdk_from_installed_dependencies(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 12))
        installed = distribution("provider-core")
        lock = (Path(__file__).resolve().parents[2] / "dependencies/provider-core-api.lock").read_text()
        pins = re.findall(r"^provider-core==([^\s\\]+)", lock, re.MULTILINE)
        self.assertEqual(len(pins), 1, "The API lock must contain exactly one provider-core pin")
        self.assertEqual(installed.version, pins[0])

        import provider_core

        for module in ("analysis", "controller", "delivery_client", "runner_session"):
            import_module("provider_core." + module)

        self.assertEqual(
            Path(provider_core.__file__).resolve(),
            installed.locate_file("provider_core/__init__.py").resolve(),
            "The imported SDK must belong to the installed release",
        )
        checked = subprocess.run(
            (sys.executable, "-m", "pip", "check"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.assertEqual(checked.returncode, 0, checked.stdout)
