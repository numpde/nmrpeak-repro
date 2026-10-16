"""Keep a published experiment verifiable without models, GPUs, or mutable reports."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmark_tools import archive, host

EXPERIMENT = Path(__file__).resolve().parents[2] / "benchmark/20261015T065506.188575Z-4331147244aa"


def seal(directory):
    (directory / "SHA256SUMS").write_text("".join(
        f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(directory).as_posix()}\n"
        for path in sorted(directory.rglob("*")) if path.is_file() and path.name != "SHA256SUMS"))


class BenchmarkArchiveTests(unittest.TestCase):
    def test_published_experiment_reproduces_without_modifying_evidence(self):
        before = {path: path.read_bytes() for path in EXPERIMENT.rglob("*") if path.is_file()}
        summary = archive.verify_archive(EXPERIMENT)
        self.assertEqual(sum(row["samples"] for row in summary["latencies"]), 480)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_changed_observation_is_rejected_by_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "experiment"
            shutil.copytree(EXPERIMENT, directory)
            with (directory / "logs/hf-cpu.jsonl").open("a") as stream:
                stream.write("\n")
            with self.assertRaisesRegex(ValueError, "Checksum mismatch for logs/hf-cpu.jsonl"):
                archive.verify_archive(directory)

    def test_resealed_but_incorrect_results_and_missing_observations_are_rejected(self):
        for change, reason in (("report", "Recomputed comparison.md differs"),
                               ("output", "experiment is incomplete"),
                               ("corpus", "Corpus identity differs")):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "experiment"
                shutil.copytree(EXPERIMENT, directory)
                if change == "report":
                    (directory / "comparison.md").write_text("A fabricated performance claim\n")
                elif change == "output":
                    path = directory / "logs/hf-cpu.jsonl"
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    rows = [row for row in rows if not (row["type"] == "output" and row["phase"] == "measured")]
                    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                else:
                    (directory / "cases.json").write_text("{}\n")
                seal(directory)
                with self.assertRaisesRegex(ValueError, reason):
                    archive.verify_archive(directory)

    def test_system_inventory_selects_scientific_fields(self):
        with patch.object(host.platform, "freedesktop_os_release", return_value={
                "ID": "linux", "VERSION_ID": "1", "PRETTY_NAME": "Example Linux", "PRIVATE": "secret"}), \
             patch.object(host.platform, "system", return_value="Linux"), \
             patch.object(host.platform, "release", return_value="6.1"), \
             patch.object(host.platform, "machine", return_value="x86_64"), \
             patch.object(host.Path, "read_text", return_value="MemTotal: 1024 kB\nSwapTotal: 0 kB\n"), \
             patch.object(host, "docker", return_value=SimpleNamespace(stdout=b"29.6.1\n")) as docker:
            facts = host.system_metadata()
        self.assertEqual(facts["memory_total_bytes"], 1024 * 1024)
        self.assertEqual(facts["swap_total_bytes"], 0)
        self.assertEqual(facts["os"], {"ID": "linux", "VERSION_ID": "1", "PRETTY_NAME": "Example Linux"})
        self.assertEqual(facts["kernel"], {"system": "Linux", "release": "6.1", "machine": "x86_64"})
        docker.assert_called_once_with("version", "--format", "{{.Server.Version}}", stdout=host.subprocess.PIPE)


if __name__ == "__main__":
    unittest.main()
