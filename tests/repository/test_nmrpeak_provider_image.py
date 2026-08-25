"""Prove the committed provider image context is the provider runtime alone."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from repository_checks.nmrpeak_image_inputs import materialize_image_context


REPOSITORY_ROOT = Path(__file__).parents[2]


class NmrpeakProviderImageTests(unittest.TestCase):
    def test_committed_context_contains_exactly_the_declared_provider_inputs(self) -> None:
        revision = subprocess.run(
            ("git", "-C", str(REPOSITORY_ROOT), "rev-parse", "--verify", "HEAD"),
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        with tempfile.TemporaryDirectory() as temporary:
            context = Path(temporary)
            identity = materialize_image_context(
                REPOSITORY_ROOT,
                revision,
                context,
                "provider",
            )
            files = {
                path.relative_to(context).as_posix()
                for path in context.rglob("*")
                if path.is_file()
            }

        declared = set(
            (REPOSITORY_ROOT / "containers/provider/image-inputs.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(files, declared)
        self.assertRegex(identity, r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
