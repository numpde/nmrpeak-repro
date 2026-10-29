"""Prove benchmark measurements, retained failures, and container authority without a GPU."""
from __future__ import annotations

import io
import json
import os
import signal
import stat
from contextlib import nullcontext
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmark_tools import host, report, worker

GPU = "GPU-75078110-7bdd-beaa-2195-2ba33a555cd7"


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_lock_stage_uses_selected_proxy_for_build_and_renderer(self):
        calls = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("containers/runner-lock/seed-and-compile.sh",
                             "families/nmrpeak/targets/cpu-x86_64/Dockerfile.lock",
                             "families/nmrpeak/targets/cpu-x86_64/Dockerfile.lock.dockerignore",
                             "families/nmrpeak/pyproject.toml", "containers/benchmark/requirements.lock"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            def docker(*arguments, **kwargs):
                calls.append(arguments)
                if arguments[0] == "context":
                    return SimpleNamespace(stdout=b"unix:///var/run/docker.sock\n")
                if arguments[0] == "run":
                    kwargs["stdout"].write(b"fixture\n")
                return SimpleNamespace(stdout=b"")
            selected = ["--network", "host", "--build-arg", "HTTPS_PROXY=http://127.0.0.1:12345", "--build-arg", "NO_PROXY="]
            named_temporary_file = tempfile.NamedTemporaryFile
            with (patch.object(host, "ROOT", root), patch.object(host, "docker", side_effect=docker),
                  patch.object(host, "build_network", return_value=nullcontext(selected)),
                  patch.object(host.tempfile, "NamedTemporaryFile", side_effect=lambda **kwargs: named_temporary_file(dir=root, **kwargs)),
                  patch("repository_checks.nmrpeak_lock.check_lock"), patch("sys.stdout", io.StringIO())):
                host.stage_lock()
            build, run = calls[1:]
            self.assertIn("HTTPS_PROXY=http://127.0.0.1:12345", build)
            self.assertEqual(run[run.index("--network") + 1], "host")
            self.assertIn("HTTPS_PROXY=http://127.0.0.1:12345", run)
            self.assertIn("NO_PROXY=", run)

    def test_benchmark_lock_stage_preserves_inputs_after_unconfirmed_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("containers/runner-lock/seed-and-compile.sh",
                             "families/nmrpeak/targets/cpu-x86_64/Dockerfile.lock",
                             "families/nmrpeak/targets/cpu-x86_64/Dockerfile.lock.dockerignore",
                             "families/nmrpeak/pyproject.toml", "containers/benchmark/requirements.lock"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            staged = root / "staged"
            staged.mkdir()
            def docker(*arguments, **_kwargs):
                if arguments[0] == "context":
                    return SimpleNamespace(stdout=b"unix:///var/run/docker.sock\n")
                raise RuntimeError("build client lost")
            error_output = io.StringIO()
            with (patch.object(host, "ROOT", root), patch.object(host, "docker", side_effect=docker),
                  patch.object(host.tempfile, "mkdtemp", return_value=str(staged)),
                  patch.object(host, "build_network", return_value=nullcontext(["--network", "default"])),
                  patch("sys.stderr", error_output), self.assertRaisesRegex(RuntimeError, "build client lost")):
                host.stage_lock()
            self.assertTrue((staged / "pyproject.toml").is_file())
            self.assertTrue((staged / "requirements.seed").is_file())
            self.assertIn("retirement is unconfirmed", error_output.getvalue())

    def test_staged_harness_is_readable_by_the_image_user_under_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(host, "git", return_value=b"committed bytes"):
            previous = os.umask(0o077)
            try:
                harness = host.stage_harness("a" * 40, Path(temporary))
            finally:
                os.umask(previous)
            for directory in (harness, harness.parent):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o555)
            for file in harness.iterdir():
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o444)
                self.assertEqual(file.read_bytes(), b"committed bytes")

    def test_build_network_requires_a_selection_and_accepts_system(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"NETWORK_INTERFACE_SELECTED": ""}), \
             patch.object(host.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "selected network interface"):
                with host.build_network(Path(temporary)):
                    self.fail("an unspecified route must not start a build")
            os.environ["NETWORK_INTERFACE_SELECTED"] = "system"
            with host.build_network(Path(temporary)) as arguments:
                self.assertEqual(arguments[:2], ["--network", "default"])
                self.assertIn("HTTP_PROXY=", arguments)
            launch.assert_not_called()

    def test_explicit_wifi_build_route_cleans_up_after_success_failure_and_interruption(self):
        for failure in (None, RuntimeError("build failed"), KeyboardInterrupt()):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary, \
                 patch.dict(os.environ, {"NETWORK_INTERFACE_SELECTED": "wlp10s0"}), \
                 patch.object(host.subprocess, "Popen") as launch:
                scratch = Path(temporary)
                (scratch / "proxy.port").write_text("12345\n")
                try:
                    with host.build_network(scratch) as arguments:
                        self.assertEqual(arguments[:2], ["--network", "host"])
                        self.assertIn("HTTPS_PROXY=http://127.0.0.1:12345", arguments)
                        self.assertIn("--interface=wlp10s0", launch.call_args.args[0])
                        if failure is not None:
                            raise failure
                except BaseException as caught:
                    self.assertIs(caught, failure)
                launch.return_value.terminate.assert_called_once()
                launch.return_value.wait.assert_called_once_with(timeout=5)

    def test_build_proxy_failure_never_falls_back_to_default_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"NETWORK_INTERFACE_SELECTED": "missing"}), \
             patch.object(host.subprocess, "Popen") as launch:
            launch.return_value.poll.return_value = 7
            launch.return_value.returncode = 7
            with self.assertRaisesRegex(RuntimeError, "status 7"):
                with host.build_network(Path(temporary)):
                    self.fail("a failed proxy must not permit a build")
            launch.return_value.terminate.assert_called_once()

    def test_network_interface_is_deliberate_make_input(self):
        for arguments, environment, succeeds in (
            (["NETWORK_INTERFACE=system"], {}, True),
            (["NETWORK_INTERFACE="], {}, False),
            ([], {"NETWORK_INTERFACE": "system"}, False),
        ):
            for target in ("benchmark/image/build", "benchmark/lock"):
                with self.subTest(target=target, arguments=arguments, environment=environment):
                    result = subprocess.run(["make", "-C", str(host.ROOT), target, "PYTHON=/bin/true", *arguments],
                                            env={**os.environ, **environment}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(result.returncode == 0, succeeds, result.stderr.decode())

    def test_sigterm_enters_the_controlled_interruption_path(self):
        with patch.object(host.signal, "signal") as register, patch("sys.argv", ["benchmark", "devices"]), \
             patch.object(host, "devices", return_value=[]), patch("sys.stdout", io.StringIO()):
            host.main()
        signum, handler = register.call_args.args
        self.assertEqual(signum, signal.SIGTERM)
        with self.assertRaises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)

    def test_gpu_inventory_failure_retains_an_incomplete_report(self):
        options = SimpleNamespace(devices=["all"], repetitions=1, warmups=0, threads=8, cases=[], lanes=["hf"])
        with tempfile.TemporaryDirectory() as temporary, patch.object(host, "ROOT", Path(temporary)), \
             patch.object(host, "devices", side_effect=FileNotFoundError("nvidia-smi missing")):
            with self.assertRaises(FileNotFoundError):
                host.run_benchmark("a" * 40, options)
            directory, = (Path(temporary) / "benchmark").iterdir()
            self.assertIn("nvidia-smi missing", (directory / "error.log").read_text())
            self.assertEqual(json.loads((directory / "summary.json").read_text())["status"], "incomplete")

    def test_measurement_waits_for_gpu_before_and_after_the_operation(self):
        calls = []
        with patch.object(worker.time, "perf_counter", side_effect=[10, 13]):
            value, seconds = worker.measured(lambda: calls.append("generate") or ["CCO"],
                                            lambda: calls.append("sync"))
        self.assertEqual(calls, ["sync", "generate", "sync"])
        self.assertEqual((value, seconds), (["CCO"], 3))

    def test_event_stream_is_separate_from_upstream_stdout(self):
        events, upstream = io.StringIO(), io.StringIO()
        with patch.object(worker, "_EVENTS", events), patch("sys.stdout", upstream):
            print("fused_layer_norm is not installed")
            worker.emit("complete")
        self.assertEqual(json.loads(events.getvalue()), {"type": "complete"})
        self.assertIn("fused_layer_norm", upstream.getvalue())

    def test_container_has_only_selected_gpu_and_read_only_inputs(self):
        options = SimpleNamespace(threads=8, warmups=3, repetitions=20, cases=["small"])
        run = {"device": GPU, "image_id": "sha256:" + "a" * 64, "lane": "hf"}
        checkpoint = {"volume": "reviewed-checkpoint", "sha256": "sha256:" + "b" * 64}
        args = host.run_arguments(run, checkpoint, Path("/tmp/harness"), options, "owned-run")
        self.assertEqual(args[args.index("--gpus") + 1], f"device={GPU}")
        self.assertIn("CUDA_VISIBLE_DEVICES=0", args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--read-only", args)
        self.assertNotIn("--user", args)
        mounts = [args[i + 1] for i, value in enumerate(args) if value == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all("readonly" in mount for mount in mounts))
        self.assertFalse(any("docker.sock" in mount or "secrets" in mount for mount in mounts))
        self.assertIn(run["image_id"], args)
        run["device"] = "cpu"
        args = host.run_arguments(run, checkpoint, Path("/tmp/harness"), options, "owned-run")
        self.assertNotIn("--gpus", args)
        self.assertIn("CUDA_VISIBLE_DEVICES=", args)
        with self.assertRaisesRegex(ValueError, "mount"):
            host.run_arguments(run, checkpoint, Path("/tmp/harness,escape"), options, "owned-run")

    def test_timeout_cleans_up_and_retains_raw_events(self):
        run = {"id": "hf-cpu", "device": "cpu", "image_id": "sha256:" + "a" * 64, "lane": "hf"}
        options = SimpleNamespace(threads=8, warmups=0, repetitions=1, cases=[], timeout=1)
        checkpoint = {"volume": "reviewed", "sha256": "sha256:" + "b" * 64}
        def timeout(*args, **kwargs):
            kwargs["stdout"].write('{"type":"load"}\n')
            raise subprocess.TimeoutExpired("docker", 1)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "logs").mkdir()
            with patch.object(host, "activity", return_value={}), \
                 patch.object(host.subprocess, "run", side_effect=timeout), \
                 patch.object(host, "remove_interrupted_container") as cleanup, \
                 self.assertRaises(subprocess.TimeoutExpired):
                host.execute_run(run, checkpoint, Path("/tmp/harness"), options, directory, [])
            cleanup.assert_called_once()
            self.assertIn('"load"', (directory / "logs/hf-cpu.jsonl").read_text())
            self.assertIn("TimeoutExpired", run["error"])

    def test_corpus_uses_admitted_wire_inputs(self):
        from nmrpeak_provider.hf_binding import parse_hf_runner_input
        from nmrpeak_provider.chf_binding import parse_chf_runner_input
        corpus = json.loads((host.ROOT / "benchmark_tools/cases.json").read_text())
        counts = []
        for case in corpus["cases"]:
            hf = parse_hf_runner_input(case["inputs"]["hf"])
            chf = parse_chf_runner_input(case["inputs"]["chf"])
            self.assertEqual(hf.proton_peaks, chf.proton_peaks)
            counts.append(len(hf.proton_peaks))
        self.assertEqual(counts, sorted(set(counts)))

    def test_nonzero_docker_client_exit_requires_cleanup_before_the_next_lane(self):
        run = {"id": "hf-cpu", "device": "cpu", "image_id": "sha256:" + "a" * 64, "lane": "hf"}
        options = SimpleNamespace(threads=8, warmups=0, repetitions=1, cases=[], timeout=1)
        checkpoint = {"volume": "reviewed", "sha256": "sha256:" + "b" * 64}
        for cleanup_failure in (None, RuntimeError("daemon unavailable")):
            calls = []
            def cleanup_action(name):
                calls.append("cleanup")
                if cleanup_failure is not None:
                    raise cleanup_failure
            with self.subTest(cleanup_failure=cleanup_failure), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                (directory / "logs").mkdir()
                with patch.object(host, "activity", side_effect=lambda _: calls.append("activity") or {}) as activity, \
                     patch.object(host.subprocess, "run", return_value=SimpleNamespace(returncode=1)), \
                     patch.object(host, "remove_interrupted_container", side_effect=cleanup_action) as cleanup:
                    if cleanup_failure is None:
                        host.execute_run(run, checkpoint, Path("/tmp/harness"), options, directory, [])
                        self.assertEqual(activity.call_count, 2)
                        self.assertEqual(calls, ["activity", "cleanup", "activity"])
                    else:
                        with self.assertRaisesRegex(RuntimeError, "daemon unavailable"):
                            host.execute_run(run, checkpoint, Path("/tmp/harness"), options, directory, [])
                        self.assertEqual(activity.call_count, 1)
                        self.assertEqual(calls, ["activity", "cleanup"])
                        self.assertIn("daemon unavailable", run["cleanup_error"])
                    self.assertEqual(run["exit_code"], 1)
                    cleanup.assert_called_once()

    def fixture(self, directory, *, missing_output=False, malformed=False):
        manifest = {"source_revision": "a" * 40, "status": "complete", "repetitions": 1,
                    "cases": ["small"], "runs": []}
        (directory / "logs").mkdir()
        for device, seconds, candidates in (("cpu", 4, ["CCO"]), (GPU, 2, ["OCC"])):
            name = f"hf-{device}"
            manifest["runs"].append({"id": name, "lane": "hf", "device": device, "exit_code": 0,
                                     "image_id": "sha256:" + "b" * 64, "image_input_id": "sha256:" + "c" * 64})
            rows = [{"type": "load", "model_load_and_transfer_seconds": 1},
                    {"type": "case", "case": "small", "input_tokens": 15},
                    {"type": "measurement", "case": "small", "phase": "measured", "repetition": 0,
                     "elapsed_seconds": seconds, "first_generation": True, "process_peak_rss_bytes": 1024,
                     "gpu_peak_allocated_bytes": None, "gpu_peak_reserved_bytes": None}]
            if not missing_output:
                rows.append({"type": "output", "case": "small", "phase": "measured", "repetition": 0,
                             "candidates": candidates, "canonical_smiles": ["CCO"], "valid_count": 1,
                             "candidate_characters": [3]})
            rows.append({"type": "complete"})
            text = "".join(json.dumps(row) + "\n" for row in rows)
            (directory / "logs" / f"{name}.jsonl").write_text(text + ('{"type":' if malformed else ''))
        return manifest

    def test_report_separates_ranked_agreement_from_molecular_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = self.fixture(directory)
            summary = report.write_report(directory, manifest)
            self.assertEqual(summary["status"], "complete")
            comparison = summary["comparisons"][0]
            self.assertEqual(comparison["median_speed_ratio"], 2)
            self.assertEqual(comparison["output_pairs"], [{"ranked_equal": False, "molecule_overlap": 1}])
            self.assertIn("15 input tokens", (directory / "comparison.md").read_text())
            self.assertIn("first actual generation", (directory / "comparison.md").read_text())

    def test_missing_outputs_or_truncated_logs_never_report_complete(self):
        for missing, malformed in ((True, False), (False, True)):
            with self.subTest(missing=missing, malformed=malformed), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                manifest = self.fixture(directory, missing_output=missing, malformed=malformed)
                self.assertEqual(report.write_report(directory, manifest)["status"], "incomplete")
                self.assertTrue((directory / "measurements.jsonl").exists())

    def test_failed_setup_keeps_a_manifest_and_report(self):
        options = SimpleNamespace(devices=["cpu"], repetitions=1, warmups=0, threads=8, cases=[], lanes=["hf"])
        with tempfile.TemporaryDirectory() as temporary, patch.object(host, "ROOT", Path(temporary)), \
             patch.object(host, "command", side_effect=FileNotFoundError("lscpu missing")):
            with self.assertRaises(FileNotFoundError):
                host.run_benchmark("a" * 40, options)
            directory, = (Path(temporary) / "benchmark").iterdir()
            self.assertEqual(json.loads((directory / "manifest.json").read_text())["status"], "failed")
            self.assertTrue((directory / "comparison.md").exists())

    def test_primary_failure_survives_report_failure(self):
        options = SimpleNamespace(devices=["cpu"], repetitions=1, warmups=0, threads=8, cases=[], lanes=["hf"])
        with tempfile.TemporaryDirectory() as temporary, patch.object(host, "ROOT", Path(temporary)), \
             patch.object(host, "command", side_effect=FileNotFoundError("lscpu missing")), \
             patch.object(host, "write_report", side_effect=ValueError("broken report")):
            with self.assertRaises(FileNotFoundError) as raised:
                host.run_benchmark("a" * 40, options)
            self.assertTrue(any("broken report" in note for note in raised.exception.__notes__))
            directory, = (Path(temporary) / "benchmark").iterdir()
            self.assertTrue((directory / "error.log").exists())


if __name__ == "__main__":
    unittest.main()
