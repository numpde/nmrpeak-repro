"""Exercise the acquisition launcher's warm and post-acquisition decisions."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WheelhouseFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.root = root
        self.repo, self.pool, self.engine = root / "repo", root / "pool", root / "engine"
        for directory in (self.repo / "scripts", self.repo / "repository_checks",
                          self.repo / "dependencies", self.pool, self.engine):
            directory.mkdir(parents=True)
        for name in ("runner-wheelhouse.sh", "network-interface.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.repo / "scripts" / name)
        for name in ("containers/provider/Dockerfile", "containers/provider/Dockerfile.dockerignore",
                     "bootstrap/acquisition/tools/inference_native.sh"):
            destination = self.repo / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        installer = "bootstrap/acquisition/acquisition_tool/installer"
        shutil.copytree(ROOT / installer, self.repo / installer)
        (self.repo / "repository_checks/__init__.py").touch()
        (self.repo / "repository_checks/wheelhouse_tool_asset.py").write_text('TOOL_NAME = "tool.pyz"\n')
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
from pathlib import Path
import sys

arguments = sys.argv[1:]
if arguments[:2] != ["--host", "unix:///tmp/fixture.sock"]:
    raise SystemExit("unexpected Docker endpoint")
while arguments[:2] == ["--host", "unix:///tmp/fixture.sock"]:
    arguments = arguments[2:]
installer_id = "sha256:" + "b" * 64
if arguments[:2] == ["image", "inspect"]:
    image, option, template = arguments[2:]
    if option != "--format":
        raise SystemExit("unexpected Docker image inspection")
    if image == installer_id and template == "{{{{.Id}}}} {{{{.Os}}}}/{{{{.Architecture}}}}":
        print(installer_id + " linux/amd64")
    elif image.startswith("docker.io/library/python:") and template == "{{{{.Id}}}}":
        print("sha256:" + "a" * 64)
    else:
        raise SystemExit("unexpected Docker image inspection")
elif arguments[:8] == ["build", "--platform", "linux/amd64", "--pull=false",
                       "--network", "none", "--target", "installer"]:
    if len(arguments) != 13 or arguments[8] != "--iidfile" or arguments[10] != "--file":
        raise SystemExit("unexpected installer build arguments")
    context = Path(arguments[12])
    dockerfile = context / "containers/provider/Dockerfile"
    installer = context / "bootstrap/acquisition/acquisition_tool/installer"
    if Path(arguments[11]) != dockerfile or not dockerfile.is_file() or not installer.is_dir():
        raise SystemExit("installer build lacks its captured source inputs")
    Path(arguments[9]).write_text(installer_id + "\\n")
else:
    raise SystemExit("unexpected Docker command")
''')
        docker.chmod(0o755)
        self.env = os.environ | {
            'PATH': str(root) + os.pathsep + os.environ['PATH'], 'FAKE_ENGINE': str(self.engine),
            'NMRPEAK_DOCKER_HOST': 'unix:///tmp/fixture.sock',
            'NMRPEAK_WHEELHOUSE_INPUT': str(self.pool), 'NMRPEAK_WHEELHOUSE_INPUT_ORIGIN': 'command line',
            'PYTHON': sys.executable, 'TMPDIR': str(root),
        }

    def run_acquisition(self, initial, final, *, route=True, extra=None):
        env = self.env | {'FAKE_INITIAL': str(initial), 'FAKE_FINAL': str(final)}
        if extra:
            env.update(extra)
        if route:
            env |= {'NMRPEAK_NETWORK_INTERFACE_INPUT': 'system',
                    'NMRPEAK_NETWORK_INTERFACE_INPUT_ORIGIN': 'command line'}
        return subprocess.run(['bash', str(self.repo / 'scripts/runner-wheelhouse.sh'), 'provider'],
                              cwd=self.repo, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)

