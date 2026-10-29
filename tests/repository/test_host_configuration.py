"""Keep host configuration tooling independent of contained SDK runtime imports."""

from pathlib import Path
import subprocess
import sys
import unittest


class HostConfigurationTests(unittest.TestCase):
    def test_host_deployment_imports_without_the_provider_sdk(self):
        root = Path(__file__).parents[2]
        probe = '''
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class NoRuntimeSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in {"provider_core", "cryptography", "httpx", "cffi", "http_sf"}:
            raise ImportError("Host configuration must not import contained API or cryptographic dependencies")
sys.meta_path.insert(0, NoRuntimeSDK())
import nmrpeak_provider.provider_config
import nmrpeak_provider.journal_archive_record
import deployment.provider_deployment
'''
        checked = subprocess.run((sys.executable, "-P", "-c", probe, str(root)),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(checked.returncode, 0, checked.stdout)
