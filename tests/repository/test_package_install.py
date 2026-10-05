from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class PackageInstallTests(unittest.TestCase):
    def test_provider_builds_and_imports_from_an_offline_wheel(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            environment = root / "environment"
            wheels = root / "wheels"
            source.mkdir()
            clean_environment = dict(os.environ)
            clean_environment.pop("PYTHONPATH", None)
            clean_environment.pop("PYTHONHOME", None)
            shutil.copy2(repository / "pyproject.toml", source / "pyproject.toml")
            shutil.copytree(repository / "nmrpeak_provider", source / "nmrpeak_provider")
            build = subprocess.run(
                (sys.executable, "-P", "-m", "pip", "wheel", "--no-index", "--no-deps",
                 "--no-build-isolation", "--wheel-dir", str(wheels), str(source)),
                check=False,
                env=clean_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(build.returncode, 0, build.stderr.decode("utf-8", errors="replace"))
            wheel = tuple(wheels.glob("*.whl"))
            self.assertEqual(len(wheel), 1)
            created = subprocess.run(
                (sys.executable, "-P", "-m", "venv", "--system-site-packages", str(environment)),
                check=False,
                env=clean_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(created.returncode, 0, created.stderr.decode("utf-8", errors="replace"))
            installed_python = environment / "bin" / "python"
            install = subprocess.run(
                (str(installed_python), "-P", "-m", "pip", "install", "--no-index", "--no-deps",
                 str(wheel[0])),
                check=False,
                env=clean_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(install.returncode, 0, install.stderr.decode("utf-8", errors="replace"))
            checked = subprocess.run(
                (str(installed_python), "-P", "-m", "pip", "check"),
                check=False,
                env=clean_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(
                checked.returncode,
                0,
                (checked.stdout + checked.stderr).decode("utf-8", errors="replace"),
            )
            imported = subprocess.run(
                (str(installed_python), "-P", "-c",
                 "from pathlib import Path; import sys; "
                 "import nmrpeak_provider.provider_main as provider_main; "
                 "from importlib.resources import files; "
                 "root=Path(sys.prefix).resolve(); "
                 "assert Path(provider_main.__file__).resolve().is_relative_to(root); "
                 "prompt=files('nmrpeak_provider').joinpath('prompts/interpreter.md'); "
                 "policy=files('nmrpeak_provider').joinpath('policies/hf_preparation_failures.toml'); "
                 "assert Path(str(prompt)).resolve().is_relative_to(root) and prompt.is_file(); "
                 "assert Path(str(policy)).resolve().is_relative_to(root) and policy.is_file()"),
                check=False,
                cwd=root,
                env=clean_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr.decode("utf-8", errors="replace"))
