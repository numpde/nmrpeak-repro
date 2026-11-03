"""Exercise the acquisition launcher's warm and post-acquisition decisions."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WheelhouseAcquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repo, self.pool, self.engine = root / "repo", root / "pool", root / "engine"
        for directory in (self.repo / "scripts", self.repo / "repository_checks",
                          self.repo / "deployment", self.repo / "dependencies", self.pool, self.engine):
            directory.mkdir(parents=True)
        for name in ("runner-wheelhouse.sh", "network-interface.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.repo / "scripts" / name)
        (self.repo / "repository_checks/__init__.py").touch()
        (self.repo / "deployment/__init__.py").touch()
        (self.repo / "repository_checks/wheelhouse_tool_asset.py").write_text('TOOL_NAME = "tool.pyz"\n')
        (self.repo / "deployment/operator_container_uid.py").write_text('print("1000")\n')
        (self.repo / "repository_checks/image_wheel_report.py").write_text('# fixture report\n')
        (self.repo / "requirements.lock").write_text(
            '-c dependencies/provider-core-source.txt\n-r dependencies/provider-core-api.lock\nsetuptools==1.0\n'
        )
        (self.repo / "dependencies/provider-core-api.lock").write_text('provider-core==1.0\n')
        (self.repo / "dependencies/provider-core-source.txt").write_text(
            'provider-core @ https://github.com/numpde/nmr-provider-tools/releases/download/v1/'
            'provider_core-1.0-py3-none-any.whl \\\n    --hash=sha256:' + 'a' * 64 + '\n'
        )
        (self.pool / "tool.pyz").touch()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '-qm', 'fixture', '-m', 'Capture the committed acquisition inputs.'], check=True)
        docker = root / 'docker'
        docker.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
a = sys.argv[3:]
root = pathlib.Path(os.environ["FAKE_ENGINE"])
if a[:2] == ["image", "inspect"]:
    print("sha256:" + "a" * 64)
elif a[0] == "run":
    name = a[a.index("--name") + 1]
    cidfile = pathlib.Path(a[a.index("--cidfile") + 1])
    label = a[a.index("--label") + 1].split("=", 1)[1]
    phase = next(x for x in ("initial", "final", "locate", "acquire") if "-" + x + "-" in name)
    cid = str(len(list(root.glob("*.json"))) + 1) * 64
    status = int(os.environ.get("FAKE_" + phase.upper(), "0")) if phase in ("initial", "final") else 0
    cidfile.write_text(cid)
    (root / (cid + ".json")).write_text(json.dumps([status, label]))
    with (root / "events").open("a") as log:
        log.write(phase + " " + (a[a.index("--network") + 1]) + "\\n")
    print("{{}}")
    sys.exit(status)
elif a[0] == "inspect":
    status, label = json.loads((root / (a[1] + ".json")).read_text())
    running = "true" if os.environ.get("FAKE_FINAL_UNCERTAIN") and "-final" in label else "false"
    print(running, status, label)
elif a[0] == "rm":
    with (root / "removed").open("a") as removed:
        removed.write(a[1] + "\\n")
else:
    raise SystemExit("unexpected Docker command: " + repr(a))
''')
        docker.chmod(0o755)
        self.env = os.environ | {
            'PATH': str(root) + os.pathsep + os.environ['PATH'], 'FAKE_ENGINE': str(self.engine),
            'NMRPEAK_DOCKER_HOST': 'unix:///tmp/fixture.sock',
            'NMRPEAK_WHEELHOUSE_INPUT': str(self.pool), 'NMRPEAK_WHEELHOUSE_INPUT_ORIGIN': 'command line',
            'PYTHON': sys.executable, 'TMPDIR': str(root),
        }

    def run_acquisition(self, initial, final, *, route=True, uncertain=False):
        env = self.env | {'FAKE_INITIAL': str(initial), 'FAKE_FINAL': str(final)}
        if uncertain:
            env['FAKE_FINAL_UNCERTAIN'] = '1'
        if route:
            env |= {'NMRPEAK_NETWORK_INTERFACE_INPUT': 'system',
                    'NMRPEAK_NETWORK_INTERFACE_INPUT_ORIGIN': 'command line'}
        return subprocess.run(['bash', str(self.repo / 'scripts/runner-wheelhouse.sh'), 'provider'],
                              cwd=self.repo, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def test_complete_pool_returns_before_route_or_acquisition(self):
        result = self.run_acquisition(0, 0, route=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.engine / 'events').read_text().splitlines(), ['initial none'])
        self.assertIn('no acquisition is needed', result.stdout)

    def test_acquired_pool_is_rechecked_and_failure_is_not_success(self):
        result = self.run_acquisition(1, 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('locked wheel selection still fails offline', result.stderr)
        self.assertNotIn('Acquired the locked', result.stdout)
        events = (self.engine / 'events').read_text().splitlines()
        self.assertEqual(events, ['initial none', 'locate bridge', 'acquire bridge',
                                  'locate bridge', 'acquire bridge', 'final none'])

    def test_acquired_pool_reports_success_only_after_offline_recheck(self):
        result = self.run_acquisition(1, 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.engine / 'events').read_text().splitlines()[-1], 'final none')
        self.assertIn('Acquired the locked', result.stdout)

    def test_unconfirmed_final_worker_is_not_removed_or_reported_as_success(self):
        result = self.run_acquisition(1, 0, uncertain=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('retirement or ownership could not be confirmed', result.stderr)
        self.assertIn('worker retirement is unconfirmed', result.stderr)
        self.assertNotIn('Acquired the locked', result.stdout)
        self.assertNotIn('6' * 64, (self.engine / 'removed').read_text().splitlines())


if __name__ == '__main__':
    unittest.main()
