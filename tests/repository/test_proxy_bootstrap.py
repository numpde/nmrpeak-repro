"""Keep the cold proxy bootstrap tied to the installed SDK release."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from repository_checks import proxy_bootstrap

ROOT = Path(__file__).resolve().parents[2]


class ProxyBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.wheel = self.root / 'provider_core-1.2.3-py3-none-any.whl'
        self.sources = {name: name.encode() for name in proxy_bootstrap.MEMBERS}
        self.write_wheel()
        self.snapshot = self.root / 'tools/bootstrap/provider_core'
        self.record = self.root / 'record.json'
        self.pin = self.root / 'source.txt'
        self.write_record()

    def write_wheel(self, *, duplicate=False):
        with zipfile.ZipFile(self.wheel, 'w') as archive:
            for name, data in self.sources.items():
                archive.writestr('provider_core/' + name, data)
            if duplicate:
                archive.writestr('provider_core/__init__.py', self.sources['__init__.py'])

    def write_record(self):
        digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        self.record.write_text(json.dumps({
            'files': {self.wheel.name: digest},
            'source_files': {f'src/provider_core/{name}': {
                'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            } for name, data in self.sources.items()},
        }))
        self.pin.write_text('provider-core @ https://github.com/numpde/nmr-provider-tools/releases/download/v1.2.3/'
                            f'{self.wheel.name} \\\n    --hash=sha256:{digest}\n')

    def invoke(self, *arguments):
        with (mock.patch.object(proxy_bootstrap, 'RECORD', self.record),
              mock.patch.object(proxy_bootstrap, 'PIN', self.pin),
              mock.patch.object(proxy_bootstrap, 'SNAPSHOT', self.snapshot),
              mock.patch.object(sys, 'argv', ['proxy_bootstrap', *map(str, arguments)])):
            proxy_bootstrap.main()

    def test_checked_in_snapshot_is_offline_and_needs_no_engine(self):
        env = os.environ | {'DOCKER_CONTEXT': 'not-a-context', 'PATH': '/usr/bin:/bin'}
        result = subprocess.run(['make', 'packages/proxy-bootstrap/check'], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_verified_wheel_refreshes_and_checks_without_the_wheel(self):
        self.invoke('--wheel', self.wheel)
        self.wheel.unlink()
        self.invoke()
        self.assertEqual({p.name for p in self.snapshot.iterdir()}, set(self.sources))

    def test_extra_files_and_leaf_symlinks_are_never_overwritten(self):
        self.invoke('--wheel', self.wheel)
        extra = self.snapshot / 'unexpected.py'
        extra.write_text('keep')
        for arguments in ((), ('--wheel', self.wheel)):
            with self.assertRaisesRegex(ValueError, 'exactly the release files'):
                self.invoke(*arguments)
        extra.unlink()
        target = self.snapshot / 'bound_http_proxy.py'
        target.unlink()
        target.symlink_to(self.wheel)
        with self.assertRaisesRegex(ValueError, 'not a symlink'):
            self.invoke('--wheel', self.wheel)
        self.assertTrue(target.is_symlink())

    def test_changed_snapshot_and_pin_are_rejected(self):
        self.invoke('--wheel', self.wheel)
        (self.snapshot / '__init__.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.invoke()
        self.pin.write_text(self.pin.read_text().replace('v1.2.3', 'v1.2.4'))
        with self.assertRaisesRegex(ValueError, 'source pin'):
            self.invoke('--wheel', self.wheel)

    def test_release_record_wheel_must_match_runtime_pin(self):
        record = json.loads(self.record.read_text())
        record['files'][self.wheel.name] = '0' * 64
        self.record.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'differs from the runtime wheel pin'):
            self.invoke('--wheel', self.wheel)
        self.assertFalse(self.snapshot.exists())

    def test_parent_symlink_and_mismatched_record_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.snapshot.parent.parent.mkdir()
        self.snapshot.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'without a symlink'):
            self.invoke('--wheel', self.wheel)
        self.assertEqual(list(outside.iterdir()), [])
        self.snapshot.parent.unlink()
        record = json.loads(self.record.read_text())
        record['source_files']['src/provider_core/__init__.py']['sha256'] = '0' * 64
        self.record.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.invoke('--wheel', self.wheel)
        self.assertFalse(self.snapshot.exists())

    def test_wrong_or_duplicate_wheel_never_publishes(self):
        self.wheel.write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'wheel does not match'):
            self.invoke('--wheel', self.wheel)
        self.assertFalse(self.snapshot.exists())
        with self.assertWarns(UserWarning):
            self.write_wheel(duplicate=True)
        self.write_record()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            self.invoke('--wheel', self.wheel)
        self.assertFalse(self.snapshot.exists())

    def test_refresh_requires_command_line_wheel(self):
        result = subprocess.run(['make', 'packages/proxy-bootstrap/refresh'], cwd=ROOT,
                                env=os.environ | {'PROVIDER_CORE_WHEEL': str(self.wheel)},
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PROVIDER_CORE_WHEEL', result.stderr)


if __name__ == '__main__':
    unittest.main()
