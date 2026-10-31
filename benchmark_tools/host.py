"""Build and run the isolated NMRPeak hardware comparison through Make."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

from benchmark_tools.report import write_report
from deployment.local_image import LocalImageSpec, resolve_local_image
from repository_checks.nmrpeak_image_inputs import materialize_image_context

ROOT = Path(__file__).resolve().parents[1]
DOCKER = Path("/usr/bin/docker")
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
GPU_UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def command(argv, *, timeout=30, **kwargs):
    environment = kwargs.pop("env", ENV)
    result = subprocess.run([str(item) for item in argv], env=environment, timeout=timeout,
                            check=True, **kwargs)
    return result


def docker(*args, **kwargs):
    host = os.environ.get("NMRPEAK_DOCKER_HOST", "")
    if not host.startswith("unix:///") or len(host) == len("unix:///") or any(char.isspace() for char in host):
        raise RuntimeError("Select a local Unix-socket Docker context through make before using Docker")
    return command([DOCKER, "--host", host, *args], env={**ENV, "NMRPEAK_DOCKER_HOST": host}, **kwargs)


def git(*args):
    return command(["git", "-C", ROOT, *args], stdout=subprocess.PIPE).stdout


def admit_source():
    if git("status", "--porcelain", "--untracked-files=normal").strip():
        raise RuntimeError("Cannot benchmark uncommitted source; commit the checkout and submodule changes first")
    revision = git("rev-parse", "HEAD").decode().strip()
    endpoint = os.environ.get("NMRPEAK_DOCKER_HOST", "")
    if not endpoint.startswith("unix:///") or len(endpoint) == len("unix:///") or any(char.isspace() for char in endpoint):
        raise RuntimeError("Cannot benchmark: select a local Unix-socket Docker context through make")
    if os.getuid() == 0 or os.getgid() == 0:
        raise RuntimeError("Cannot run the benchmark as root; use a regular Docker-enabled user")
    return revision


def devices():
    result = command(["nvidia-smi", "--query-gpu=uuid,name,pci.bus_id,memory.total,driver_version",
                      "--format=csv,noheader,nounits"], stdout=subprocess.PIPE)
    rows = []
    for line in result.stdout.decode().splitlines():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) != 5 or not GPU_UUID.fullmatch(fields[0]):
            raise ValueError(f"Cannot inventory GPUs: malformed nvidia-smi row {line!r}")
        rows.append(dict(zip(("uuid", "name", "pci_bus_id", "memory_mib", "driver"), fields)))
    if len({row["uuid"] for row in rows}) != len(rows):
        raise ValueError("Cannot inventory GPUs: duplicate GPU UUIDs")
    return rows


def activity(gpu_devices):
    facts = {"utc": datetime.now(timezone.utc).isoformat(), "load_average": os.getloadavg()}
    if gpu_devices:
        for key, query in (("gpu", "--query-gpu=uuid,utilization.gpu,memory.used,temperature.gpu"),
                           ("compute_processes", "--query-compute-apps=gpu_uuid,pid,used_memory")):
            result = command(["nvidia-smi", query, "--format=csv,noheader,nounits"], stdout=subprocess.PIPE)
            facts[key] = result.stdout.decode().splitlines()
    return facts


def system_metadata():
    """Record relevant host versions and memory without identity or environment dumps."""
    release = platform.freedesktop_os_release()
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return {
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "os": {key: release[key] for key in ("ID", "VERSION_ID", "PRETTY_NAME") if key in release},
        "kernel": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "memory_total_bytes": int(memory["MemTotal"].split()[0]) * 1024,
        "swap_total_bytes": int(memory["SwapTotal"].split()[0]) * 1024,
        "docker_server_version": docker("version", "--format", "{{.Server.Version}}",
                                        stdout=subprocess.PIPE).stdout.decode().strip(),
    }


def image_spec(role, input_id, revision):
    if role == "benchmark":
        return LocalImageSpec("nmrpeak-repro/benchmark", input_id,
                              (("io.numpde.nmrpeak.role", "benchmark"),),
                              ("python", "-m", "benchmark_tools.worker"))
    lane = "hf" if role == "nmrpeak_hf_v1" else "chf"
    return LocalImageSpec(f"numpde/nmrpeak-{lane}-runner", input_id,
                         (("io.numpde.nmrpeak.runner.ref", role),
                          ("io.numpde.nmrpeak.runner.target", "cpu-x86_64")),
                         ("/usr/local/bin/python3.12", "-P", "-m",
                          "nmrpeak_provider.native_launcher", role, f"models.{role}.runner.worker"))


@contextmanager
def build_network(scratch, retirement_uncertain=lambda: False):
    interface = os.environ.get("NETWORK_INTERFACE_SELECTED", "")
    if not interface:
        raise ValueError("Cannot build benchmark images without a selected network interface")
    if interface == "system":
        yield ["--network", "default", "--build-arg", "HTTP_PROXY=", "--build-arg", "HTTPS_PROXY=",
               "--build-arg", "http_proxy=", "--build-arg", "https_proxy=",
               "--build-arg", "NO_PROXY=", "--build-arg", "no_proxy=",
               "--build-arg", "ALL_PROXY=", "--build-arg", "all_proxy=",
               "--build-arg", "FTP_PROXY=", "--build-arg", "ftp_proxy="]
        return
    ready = scratch / "proxy.port"
    proxy = subprocess.Popen([sys.executable, str(ROOT / "docker/bound_http_proxy.py"),
                              f"--interface={interface}", "--ready-file", str(ready)], env=ENV,
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failure = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            if proxy.poll() is not None:
                raise RuntimeError(f"Cannot build benchmark images: Network proxy exited with status {proxy.returncode}")
            if time.monotonic() >= deadline:
                raise TimeoutError("Cannot build benchmark images: Network proxy did not become ready within five seconds")
            time.sleep(0.05)
        port = int(ready.read_text())
        if not 1 <= port <= 65535:
            raise ValueError(f"Cannot build benchmark images: Network proxy published port {port}")
        url = f"http://127.0.0.1:{port}"
        print(f"Benchmark dependency downloads through network interface {interface}", flush=True)
        yield ["--network", "host", "--build-arg", f"HTTP_PROXY={url}", "--build-arg", f"HTTPS_PROXY={url}",
               "--build-arg", f"http_proxy={url}", "--build-arg", f"https_proxy={url}",
               "--build-arg", "NO_PROXY=", "--build-arg", "no_proxy=",
               "--build-arg", "ALL_PROXY=", "--build-arg", "all_proxy=",
               "--build-arg", "FTP_PROXY=", "--build-arg", "ftp_proxy="]
    except BaseException as error:
        failure = error
        raise
    finally:
        if retirement_uncertain():
            print(f"Network proxy PID {proxy.pid} retained at {scratch} and may be accessible to local processes; inspect the process and Docker before cleanup.", file=sys.stderr)
        else:
            try:
                proxy.terminate()
                try:
                    proxy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait(timeout=5)
            except Exception as cleanup_error:
                if failure is None:
                    raise
                failure.add_note(f"Network proxy cleanup failed: {cleanup_error}")


def prepare_images(revision, scratch, build=False, network_arguments=()):
    images = {}
    for role in ("nmrpeak_hf_v1", "nmrpeak_chf_v1", "benchmark"):
        context = scratch / role
        context.mkdir()
        input_id = materialize_image_context(ROOT, revision, context, role)
        spec = image_spec(role, input_id, revision)
        if build:
            file = "containers/benchmark/Dockerfile" if role == "benchmark" else f"models/{role}/runner/Dockerfile.runner"
            docker("build", *network_arguments, "--build-arg", f"SOURCE_REVISION={revision}",
                   "--build-arg", f"IMAGE_INPUT_ID={input_id}", "--tag", f"{spec.repository}:{input_id[7:]}",
                   "--file", context / file, context, timeout=7200)
        image = resolve_local_image(DOCKER, spec)
        labels = json.loads(docker("image", "inspect", image.image_id, "--format", "{{json .Config.Labels}}",
                                   stdout=subprocess.PIPE).stdout)
        images[role] = {"image_id": image.image_id, "image_input_id": input_id,
                        "image_build_revision": labels.get("org.opencontainers.image.revision")}
    return images


def checkpoint(lane):
    from repository_checks import hf_checkpoint, chf_checkpoint, hf_release, chf_release
    from repository_checks.nmrpeak_source import read_source_declaration
    release_name = f"nmrpeak-zenodo-19122815-{lane}"
    source_revision, _ = read_source_declaration(ROOT / "families/nmrpeak/source-closure.paths")
    module = hf_release if lane == "hf" else chf_release
    release = module.parse_release_bytes(
        (ROOT / f"models/nmrpeak_{lane}_v1/releases/{release_name}.json").read_bytes(),
        expected_release_name=release_name, expected_source_revision=source_revision)
    verify = hf_checkpoint.verify_hf_checkpoint if lane == "hf" else chf_checkpoint.verify_chf_checkpoint
    volume = verify(ROOT, release)
    return {"volume": volume.name, "sha256": release.checkpoint_sha256, "bytes": release.checkpoint_bytes}


def mount_source(path):
    text = str(path)
    if any(character in text for character in (",", "\n", "\r", "\0")):
        raise ValueError(f"Cannot encode benchmark mount source {text!r}")
    return text


def run_arguments(run, checkpoint_info, harness, options, name):
    argv = ["run", "--rm", "--pull", "never", "--name", name,
            "--label", f"io.numpde.nmrpeak.benchmark={name}", "--init", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "256", "--cpus", "8", "--memory", "32g", "--memory-swap", "32g",
            "--shm-size", "1g",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=2g,mode=1777",
            "--mount", f'type=volume,src={checkpoint_info["volume"]},dst=/checkpoint,readonly,volume-nocopy',
            "--mount", f"type=bind,src={mount_source(harness)},dst=/harness,readonly",
            "--env", "PYTHONPATH=/harness:/opt/nmrpeak:/opt/unicore",
            "--env", "PYTHONHASHSEED=1", "--env", "CUBLAS_WORKSPACE_CONFIG=:4096:8",
            "--env", f"OMP_NUM_THREADS={options.threads}", "--env", f"MKL_NUM_THREADS={options.threads}"]
    gpu = run["device"] not in {"cpu", "baseline-cpu"}
    if gpu:
        if not GPU_UUID.fullmatch(run["device"]):
            raise ValueError(f'Cannot expose malformed GPU UUID {run["device"]!r}')
        argv += ["--gpus", f'device={run["device"]}', "--env", "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
                 "--env", "CUDA_VISIBLE_DEVICES=0"]
    else:
        argv += ["--env", "CUDA_VISIBLE_DEVICES=", "--env", "NVIDIA_VISIBLE_DEVICES=void"]
    argv += ["--entrypoint", "python", run["image_id"], "-m", "benchmark_tools.worker",
             "--lane", run["lane"], "--device", "cuda" if gpu else "cpu",
             "--checkpoint", checkpoint_info["sha256"], "--corpus", "/harness/benchmark_tools/cases.json",
             "--threads", str(options.threads), "--warmups", str(options.warmups),
             "--repetitions", str(options.repetitions)]
    if run["device"] == "baseline-cpu":
        argv += ["--baseline"]
    for case in options.cases:
        argv += ["--case", case]
    return argv


def remove_interrupted_container(name):
    # Inventory all containers to distinguish absence from an inspection failure.
    ids = docker("ps", "-aq", "--filter", f"label=io.numpde.nmrpeak.benchmark={name}",
                 stdout=subprocess.PIPE).stdout.decode().split()
    for container_id in ids:
        document = json.loads(docker("inspect", container_id, stdout=subprocess.PIPE).stdout)[0]
        if document["Config"]["Labels"].get("io.numpde.nmrpeak.benchmark") != name:
            raise RuntimeError("Cannot clean up interrupted benchmark: container ownership changed")
        docker("rm", "-f", document["Id"])


def execute_run(run, checkpoint_info, harness, options, directory, gpu_devices):
    name = "nmrpeak-benchmark-" + uuid.uuid4().hex
    run["activity_before"] = activity(gpu_devices)
    print(f'Benchmark {run["id"]}', flush=True)
    with (directory / "logs" / f'{run["id"]}.jsonl').open("x") as output, \
         (directory / "logs" / f'{run["id"]}.stderr.log').open("x") as errors:
        try:
            result = docker(*run_arguments(run, checkpoint_info, harness, options, name),
                            timeout=options.timeout, stdout=output, stderr=errors)
            run["exit_code"] = result.returncode
        except BaseException as error:
            run["error"] = f"{type(error).__name__}: {error}"
            try:
                remove_interrupted_container(name)
            except Exception as cleanup_error:
                error.add_note(f"Benchmark container cleanup failed: {cleanup_error}")
            raise
    if run["exit_code"] != 0:
        # An attach/client failure does not establish that daemon-side work stopped.
        try:
            remove_interrupted_container(name)
        except Exception as cleanup_error:
            run["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
            raise
    run["activity_after"] = activity(gpu_devices)


def stage_harness(revision, scratch):
    harness = scratch / "harness" / "benchmark_tools"
    harness.mkdir(parents=True)
    for name in ("__init__.py", "worker.py", "cases.json"):
        target = harness / name
        target.write_bytes(git("show", f"{revision}:benchmark_tools/{name}"))
        target.chmod(0o444)
    harness.chmod(0o555)
    harness.parent.chmod(0o555)
    return harness


def run_benchmark(revision, options):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    if (ROOT / "benchmark").is_symlink():
        raise ValueError("Cannot save benchmark results through a symlinked benchmark directory")
    directory = ROOT / "benchmark" / f"{stamp}-{revision[:12]}"
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "logs").mkdir()
    print(f"Benchmark results: {directory}", flush=True)
    manifest = {"source_revision": revision, "status": "preparing", "runs": [], "gpus": [],
                "repetitions": options.repetitions, "warmups": options.warmups, "threads": options.threads,
                "cpu_limit": 8, "memory_limit_bytes": 32 * 1024**3}
    failure = None
    try:
        all_gpus = devices() if options.devices != ["cpu"] else []
        manifest["gpus"] = all_gpus
        selected = [row for row in all_gpus if options.devices == ["all"] or row["uuid"] in options.devices]
        unknown = set(options.devices) - {"all", "cpu"} - {row["uuid"] for row in all_gpus}
        if unknown or (options.devices == ["all"] and not selected):
            raise ValueError(f"Cannot select benchmark GPUs: unavailable selectors {sorted(unknown) or ['all (no GPUs found)']}")
        if "all" in options.devices and options.devices != ["all"]:
            raise ValueError("Cannot combine all with individual benchmark device selectors")
        manifest["host"] = json.loads(command(["lscpu", "--json"], stdout=subprocess.PIPE).stdout)
        manifest["system"] = system_metadata()
        with tempfile.TemporaryDirectory(prefix="nmrpeak-benchmark-") as temporary:
            scratch = Path(temporary)
            images = prepare_images(revision, scratch)
            harness = stage_harness(revision, scratch)
            corpus_bytes = (harness / "cases.json").read_bytes()
            (directory / "cases.json").write_bytes(corpus_bytes)
            corpus = json.loads(corpus_bytes)
            available = [case["id"] for case in corpus["cases"]]
            if set(options.cases) - set(available):
                raise ValueError(f"Cannot select cases {options.cases!r}; available cases are {available!r}")
            manifest["cases"] = options.cases or available
            from hashlib import sha256
            manifest["corpus_sha256"] = "sha256:" + sha256(corpus_bytes).hexdigest()
            manifest["images"] = images
            checkpoints = {lane: checkpoint(lane) for lane in options.lanes}
            manifest["checkpoints"] = checkpoints
            requested = ["baseline-cpu", "cpu"] + [row["uuid"] for row in selected]
            manifest["runs"] = [dict(id=f"{lane}-{device}", lane=lane, device=device,
                                      **images[f"nmrpeak_{lane}_v1" if device == "baseline-cpu" else "benchmark"])
                                for device in requested for lane in options.lanes]
            manifest["status"] = "running"
            for run in manifest["runs"]:
                write_report(directory, manifest)
                execute_run(run, checkpoints[run["lane"]], harness.parent, options, directory, all_gpus)
            manifest["status"] = "complete" if all(run.get("exit_code") == 0 for run in manifest["runs"]) else "failed"
    except BaseException as error:
        failure = error
        manifest["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        manifest["error"] = f"{type(error).__name__}: {error}"
        for path, content in ((directory / "manifest.json", json.dumps(manifest, indent=2)),
                              (directory / "error.log", traceback.format_exc())):
            try:
                path.write_text(content)
            except OSError as write_error:
                error.add_note(f"Could not retain {path}: {write_error}")
        raise
    finally:
        manifest.setdefault("cases", options.cases)
        try:
            summary = write_report(directory, manifest)
            print(f'Benchmark {summary["status"]}: {directory / "comparison.md"}', flush=True)
        except Exception as report_error:
            if failure is None:
                raise
            failure.add_note(f"Could not finalize benchmark report: {report_error}")
    if summary["status"] != "complete":
        raise RuntimeError(f"Benchmark has incomplete or failed runs; inspect {directory}")


def stage_lock():
    endpoint = os.environ.get("NMRPEAK_DOCKER_HOST", "")
    if not endpoint.startswith("unix:///") or len(endpoint) == len("unix:///") or any(char.isspace() for char in endpoint):
        raise RuntimeError("Cannot stage benchmark lock: select a local Unix-socket Docker context through make")
    temporary = tempfile.mkdtemp(prefix="nmrpeak-benchmark-lock-")
    engine_active = False
    try:
        root = Path(temporary)
        for path in ("containers/runner-lock/seed-and-compile.sh",
                     "families/nmrpeak/targets/cpu-x86_64/Dockerfile.lock",
                     "families/nmrpeak/targets/cpu-x86_64/Dockerfile.lock.dockerignore"):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, target)
        shutil.copyfile(ROOT / "families/nmrpeak/pyproject.toml", root / "pyproject.toml")
        shutil.copyfile(ROOT / "containers/benchmark/requirements.lock", root / "requirements.seed")
        with build_network(root, lambda: engine_active) as build_arguments:
            engine_active = True
            docker("build", *build_arguments, "--tag", "nmrpeak-benchmark-lock:renderer", "--file",
                   root / "families/nmrpeak/targets/cpu-x86_64/Dockerfile.lock", root, timeout=600)
            engine_active = False
            run_network = "host" if build_arguments[1] == "host" else "bridge"
            proxy_values = [build_arguments[i + 1] for i, value in enumerate(build_arguments[:-1]) if value == "--build-arg"]
            proxy_arguments = [item for value in proxy_values for item in ("--env", value)]
            with tempfile.NamedTemporaryFile(prefix="nmrpeak-benchmark-", suffix=".lock", delete=False) as output:
                engine_active = True
                docker("run", "--rm", "--read-only", "--network", run_network, *proxy_arguments, "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges:true", "--pids-limit", "64", "--cpus", "2", "--memory", "8g",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=6g", "--tmpfs", "/out:rw,size=32m",
                   "--mount", f'type=bind,src={root / "pyproject.toml"},dst=/work/pyproject.toml,readonly',
                   "--mount", f'type=bind,src={root / "requirements.seed"},dst=/seed/requirements.lock,readonly',
                   "--workdir", "/work", "nmrpeak-benchmark-lock:renderer", "pyproject.toml", "--group", "benchmark-cu128",
                   "--custom-compile-command", "make benchmark/lock", "--output-file", "/out/requirements.lock",
                   stdout=output, timeout=1800)
                engine_active = False
                candidate = Path(output.name)
        from repository_checks.nmrpeak_lock import check_lock
        check_lock(ROOT, "benchmark-cu128", candidate)
        print(f"Candidate benchmark lock: {candidate}; review before replacing containers/benchmark/requirements.lock")
    except BaseException:
        if engine_active:
            print(f"Build or renderer retirement is unconfirmed; preserve inputs at {temporary} and inspect Docker before retrying.", file=sys.stderr)
        else:
            print(f"Benchmark lock staging failed; inputs remain at {temporary}.", file=sys.stderr)
        raise
    else:
        shutil.rmtree(temporary)


def main():
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f"Received signal {signum}")
    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "run", "devices", "lock"))
    options = parser.parse_args()
    if options.action == "devices":
        print(json.dumps(devices(), indent=2))
        return
    if options.action == "lock":
        stage_lock()
        return
    revision = admit_source()
    if options.action == "build":
        from repository_checks.nmrpeak_lock import check_lock
        check_lock(ROOT, "benchmark-cu128", ROOT / "containers/benchmark/requirements.lock")
        temporary = tempfile.mkdtemp(prefix="nmrpeak-benchmark-build-")
        try:
            with build_network(Path(temporary), lambda: sys.exc_info()[0] is not None) as network_arguments:
                print(json.dumps(prepare_images(revision, Path(temporary), build=True,
                                                network_arguments=network_arguments), indent=2))
        except BaseException:
            print(f"Build retirement is unconfirmed; preserve inputs at {temporary} and inspect Docker before retrying.", file=sys.stderr)
            raise
        else:
            shutil.rmtree(temporary)
        return
    options.devices = os.environ.get("BENCHMARK_DEVICES", "all").split()
    options.lanes = os.environ.get("BENCHMARK_MODELS", "hf chf").split()
    options.cases = os.environ.get("BENCHMARK_CASES", "").split()
    options.repetitions = int(os.environ.get("BENCHMARK_REPETITIONS", "20"))
    options.warmups = int(os.environ.get("BENCHMARK_WARMUPS", "3"))
    options.threads = int(os.environ.get("BENCHMARK_THREADS", "8"))
    options.timeout = int(os.environ.get("BENCHMARK_TIMEOUT", "7200"))
    if (not options.devices or not options.lanes or set(options.lanes) - {"hf", "chf"} or
        len(set(options.devices)) != len(options.devices) or len(set(options.lanes)) != len(options.lanes) or
        len(set(options.cases)) != len(options.cases) or options.repetitions < 1 or options.warmups < 0 or
        not 1 <= options.threads <= 8 or options.timeout < 1):
        parser.error("select unique devices/cases and hf/chf models; repetitions/timeout > 0, warmups >= 0, threads 1..8")
    run_benchmark(revision, options)


if __name__ == "__main__":
    main()
