"""Prove the committed benchmark image context and dependency intent."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from repository_checks.nmrpeak_image_inputs import materialize_image_context, SOURCE_CLOSURES
from repository_checks.nmrpeak_source import read_source_manifest
from repository_checks.nmrpeak_lock import check_lock

ROOT = Path(__file__).parents[2]


class BenchmarkImageTests(unittest.TestCase):
    def test_context_is_exactly_declared_files_and_verified_upstream_closures(self):
        revision = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                  check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        declared = set((ROOT / "containers/benchmark/image-inputs.txt").read_text().splitlines())
        for upstream, _, manifest in SOURCE_CLOSURES:
            declared.update(f"{upstream}/{path}" for path in read_source_manifest(ROOT / manifest))
        with tempfile.TemporaryDirectory() as temporary:
            context = Path(temporary)
            identity = materialize_image_context(ROOT, revision, context, "benchmark")
            actual = {path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()}
            self.assertEqual(actual, declared)
            self.assertRegex(identity, r"^sha256:[0-9a-f]{64}$")
            self.assertFalse(any(path.split("/")[0] in {"secrets", "weights", "benchmark", ".git", ".aws"} for path in actual))
            self.assertEqual((context / "benchmark_tools/worker.py").read_bytes(),
                             subprocess.run(["git", "-C", str(ROOT), "show", f"{revision}:benchmark_tools/worker.py"],
                                            check=True, stdout=subprocess.PIPE).stdout)

    def test_benchmark_lock_matches_shared_family_intent(self):
        check_lock(ROOT, "benchmark-cu128", ROOT / "containers/benchmark/requirements.lock")

    def test_all_model_images_share_the_builder_wide_package_cache(self):
        for relative in ("containers/benchmark/Dockerfile",
                         "models/nmrpeak_hf_v1/runner/Dockerfile.runner",
                         "models/nmrpeak_chf_v1/runner/Dockerfile.runner"):
            with self.subTest(image=relative):
                lines = (ROOT / relative).read_text().splitlines()
                cache_mounts = [line.strip().removesuffix("\\").strip() for line in lines
                                if "--mount=type=cache" in line]
                self.assertEqual(cache_mounts, ["--mount=type=cache,target=/root/.cache/uv,sharing=locked"])
                self.assertIn("--require-hashes", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
