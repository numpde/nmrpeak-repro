"""Import and recover one lane-owned Docker checkpoint volume."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import zipfile

from nmrpeak_provider.canonical_json import canonical_json_bytes
from repository_checks.checkpoint_release import (
    CheckpointRelease,
    CheckpointReleaseSpec,
    verify_release_bytes,
)
from repository_checks.nmrpeak_source import read_nmrpeak_source_revision


DOCKER_BINARY = Path("/usr/bin/docker")
DOCKER_CONTEXT = "default"
IMPORTER_IMAGE = (
    "docker.io/library/python:3.12.12-slim-bookworm@"
    "sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
)
IMPORTER_SCRIPT = "docker/checkpoint_volume.py"
OWNER_LABEL = "io.github.numpde.nmrpeak.owner"
SCHEMA_LABEL = "io.github.numpde.nmrpeak.schema"
RUNNER_LABEL = "io.github.numpde.nmrpeak.runner"
CHECKPOINT_LABEL = "io.github.numpde.nmrpeak.checkpoint"
NONCE_LABEL = "io.github.numpde.nmrpeak.operation"
OWNER_LABEL_VALUE = "github.com/numpde/nmrpeak-repro"
_NONCE = re.compile(r"[0-9a-f]{32}")
_ENGINE_OUTPUT_LIMIT = 1_048_576


class CheckpointOperationRejected(RuntimeError):
    """The checkpoint volume operation cannot prove its authority or outcome."""


@dataclass(frozen=True, slots=True)
class CheckpointVolume:
    """One inspected Docker volume and its retained creation nonce."""

    name: str
    checkpoint_sha256: str
    operation_nonce: str


@dataclass(frozen=True, slots=True)
class CheckpointImportSpec:
    """Code-owned release and engine identity for one checkpoint lane."""

    lane_name: str
    release_directory: Path
    volume_schema_id: str
    volume_prefix: str
    release: CheckpointReleaseSpec


def import_checkpoint(
    spec: CheckpointImportSpec,
    repository_root: Path,
    archive: Path,
    release_name: str,
    *,
    docker_binary: Path = DOCKER_BINARY,
    runtime_directory: Path | None = None,
) -> CheckpointVolume:
    """Verify a committed release, then admit its bytes into one Docker volume."""

    helper_path = _admit_clean_repository(spec, repository_root)
    release = _admit_release(spec, repository_root, archive, release_name)
    volume_name = checkpoint_volume_name(spec, release.checkpoint_sha256)
    with _volume_lock(spec, volume_name, runtime_directory):
        existing = _find_volume(spec, docker_binary, volume_name)
        if existing is not None:
            _run_volume_helper(
                docker_binary,
                helper_path,
                spec,
                existing,
                release,
                "verify",
            )
            return existing

        nonce = secrets.token_hex(16)
        created = CheckpointVolume(
            volume_name,
            release.checkpoint_sha256,
            nonce,
        )
        try:
            observed = _create_volume(spec, docker_binary, created)
            _populate_volume(
                docker_binary,
                helper_path,
                spec,
                observed,
                release,
                archive,
            )
            _run_volume_helper(
                docker_binary,
                helper_path,
                spec,
                created,
                release,
                "verify",
            )
        except BaseException as primary_error:
            try:
                _remove_failed_import(spec, docker_binary, created)
            except BaseException as cleanup_error:
                primary_error.add_note(
                    "The newly created checkpoint volume could not be removed; "
                    f"recover it explicitly as {created.name}."
                )
                primary_error.add_note(f"Cleanup failure: {cleanup_error}")
            raise
        return created


def recover_checkpoint(
    spec: CheckpointImportSpec,
    volume_name: str,
    confirmation: str,
    *,
    repository_root: Path | None = None,
    docker_binary: Path = DOCKER_BINARY,
    runtime_directory: Path | None = None,
) -> None:
    """Remove only confirmed, unattached, markerless import residue."""

    _validate_volume_name(spec, volume_name)
    if confirmation != volume_name:
        raise CheckpointOperationRejected(
            "checkpoint recovery confirmation must equal the full volume name"
        )
    with _volume_lock(spec, volume_name, runtime_directory):
        root = repository_root or Path(__file__).resolve().parents[1]
        helper_path = _admit_clean_repository(spec, root)
        volume = _find_volume(spec, docker_binary, volume_name)
        if volume is None:
            raise CheckpointOperationRejected(
                "checkpoint recovery volume does not exist"
            )
        _require_unattached(docker_binary, volume.name)
        _run_volume_helper(
            docker_binary,
            helper_path,
            spec,
            volume,
            None,
            "recoverable",
        )
        current = _inspect_volume(spec, docker_binary, volume.name)
        if current != volume:
            raise CheckpointOperationRejected(
                "checkpoint recovery volume identity changed before removal"
            )
        _require_unattached(docker_binary, volume.name)
        result = _docker(
            docker_binary,
            "volume",
            "rm",
            volume.name,
        )
        if result.stdout.decode("utf-8", errors="strict").strip() != volume.name:
            raise CheckpointOperationRejected(
                "Docker did not confirm the exact recovered volume name"
            )


def verify_checkpoint_volume(
    spec: CheckpointImportSpec,
    repository_root: Path,
    release: CheckpointRelease,
    *,
    docker_binary: Path = DOCKER_BINARY,
    runtime_directory: Path | None = None,
) -> CheckpointVolume:
    """Reprove one imported volume's labels, marker, and checkpoint bytes."""

    if type(release) is not CheckpointRelease:
        raise TypeError("Checkpoint verification requires one admitted release")
    helper_path = _admit_clean_repository(spec, repository_root)
    volume_name = checkpoint_volume_name(spec, release.checkpoint_sha256)
    with _volume_lock(spec, volume_name, runtime_directory):
        volume = _find_volume(spec, docker_binary, volume_name)
        if volume is None:
            raise CheckpointOperationRejected(
                f"{spec.lane_name} checkpoint volume is not imported"
            )
        _run_volume_helper(
            docker_binary,
            helper_path,
            spec,
            volume,
            release,
            "verify",
        )
        return volume


def checkpoint_volume_name(
    spec: CheckpointImportSpec,
    checkpoint_sha256: str,
) -> str:
    """Derive the complete daemon-global volume name from checkpoint bytes."""

    if (
        type(checkpoint_sha256) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", checkpoint_sha256) is None
    ):
        raise CheckpointOperationRejected(
            "checkpoint volume requires an exact SHA-256 identity"
        )
    return spec.volume_prefix + checkpoint_sha256.removeprefix("sha256:")


def _admit_release(
    spec: CheckpointImportSpec,
    repository_root: Path,
    archive: Path,
    release_name: str,
) -> CheckpointRelease:
    root = repository_root.resolve(strict=True)
    if root != repository_root or not root.is_dir():
        raise CheckpointOperationRejected(
            "checkpoint import repository root must be one resolved directory"
        )
    declaration_path = (
        root / spec.release_directory / f"{release_name}.json"
    )
    _require_tracked_path(
        root,
        declaration_path.relative_to(root).as_posix(),
        f"{spec.lane_name} release declaration",
    )
    declaration = _read_regular_file(
        declaration_path,
        f"{spec.lane_name} release declaration",
    )
    try:
        source_revision = read_nmrpeak_source_revision(
            root / "families/nmrpeak/source-closure.paths"
        )
        return verify_release_bytes(
            spec.release,
            declaration,
            archive,
            expected_release_name=release_name,
            expected_source_revision=source_revision,
        )
    except (OSError, ValueError) as error:
        raise CheckpointOperationRejected(
            "checkpoint import release or archive is not admitted"
    ) from error


def _admit_clean_repository(
    spec: CheckpointImportSpec,
    repository_root: Path,
) -> Path:
    try:
        root = repository_root.resolve(strict=True)
    except OSError as error:
        raise CheckpointOperationRejected(
            "checkpoint repository does not exist"
        ) from error
    if root != repository_root or not root.is_dir():
        raise CheckpointOperationRejected(
            "checkpoint repository must be one resolved directory"
        )
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise CheckpointOperationRejected(
            "checkpoint operation requires clean committed repository bytes"
        )
    _require_tracked_path(
        root,
        IMPORTER_SCRIPT,
        f"{spec.lane_name} checkpoint importer",
    )
    _require_tracked_path(
        root,
        "families/nmrpeak/source-closure.paths",
        "NMRPeak source declaration",
    )
    helper_path = root / IMPORTER_SCRIPT
    _read_regular_file(helper_path, f"{spec.lane_name} checkpoint importer")
    return helper_path


def _require_tracked_path(
    repository_root: Path,
    relative_path: str,
    description: str,
) -> None:
    tracked = _git(
        repository_root,
        "ls-files",
        "--error-unmatch",
        "--",
        relative_path,
    )
    if tracked != (relative_path + "\n").encode("utf-8"):
        raise CheckpointOperationRejected(
            f"{description} is not one exact tracked path"
        )


def _read_regular_file(path: Path, description: str) -> bytes:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CheckpointOperationRejected(f"{description} does not exist") from error
    if resolved != path or not stat.S_ISREG(metadata.st_mode):
        raise CheckpointOperationRejected(
            f"{description} must be one resolved non-symlink regular file"
        )
    return path.read_bytes()


def _find_volume(
    spec: CheckpointImportSpec,
    docker_binary: Path,
    volume_name: str,
) -> CheckpointVolume | None:
    records = _json_lines(
        _docker(
            docker_binary,
            "volume",
            "ls",
            "--filter",
            f"name=^{volume_name}$",
            "--format",
            "{{json .}}",
        ).stdout,
        "Docker volume inventory",
    )
    names: list[str] = []
    for record in records:
        if type(record) is not dict or type(record.get("Name")) is not str:
            raise CheckpointOperationRejected(
                "Docker volume inventory has an invalid record"
            )
        names.append(record["Name"])
    if not names:
        return None
    if names != [volume_name]:
        raise CheckpointOperationRejected(
            "Docker volume inventory is ambiguous for the exact checkpoint name"
        )
    return _inspect_volume(spec, docker_binary, volume_name)


def _create_volume(
    spec: CheckpointImportSpec,
    docker_binary: Path,
    expected: CheckpointVolume,
) -> CheckpointVolume:
    labels = _volume_labels(spec, expected)
    arguments = ["volume", "create", "--driver", "local"]
    for name, value in sorted(labels.items()):
        arguments.extend(("--label", f"{name}={value}"))
    arguments.append(expected.name)
    result = _docker(docker_binary, *arguments)
    if result.stdout.decode("utf-8", errors="strict").strip() != expected.name:
        raise CheckpointOperationRejected(
            "Docker did not confirm the exact created checkpoint volume"
        )
    observed = _inspect_volume(spec, docker_binary, expected.name)
    if observed != expected:
        raise CheckpointOperationRejected(
            "created checkpoint volume differs from this import invocation"
        )
    return observed


def _inspect_volume(
    spec: CheckpointImportSpec,
    docker_binary: Path,
    volume_name: str,
) -> CheckpointVolume:
    document = _single_json_document(
        _docker(docker_binary, "volume", "inspect", volume_name).stdout,
        "Docker checkpoint volume inspection",
    )
    if type(document) is not list or len(document) != 1 or type(document[0]) is not dict:
        raise CheckpointOperationRejected(
            "Docker checkpoint volume inspection has an invalid shape"
        )
    record = document[0]
    if record.get("Name") != volume_name or record.get("Driver") != "local":
        raise CheckpointOperationRejected(
            "Docker checkpoint volume has an unexpected name or driver"
        )
    labels = record.get("Labels")
    if type(labels) is not dict or any(
        type(name) is not str or type(value) is not str
        for name, value in labels.items()
    ):
        raise CheckpointOperationRejected(
            "Docker checkpoint volume labels have an invalid shape"
        )
    checkpoint_sha256 = labels.get(CHECKPOINT_LABEL)
    nonce = labels.get(NONCE_LABEL)
    if (
        type(checkpoint_sha256) is not str
        or checkpoint_volume_name(spec, checkpoint_sha256) != volume_name
        or type(nonce) is not str
        or _NONCE.fullmatch(nonce) is None
    ):
        raise CheckpointOperationRejected(
            "Docker checkpoint volume identity labels are invalid"
        )
    volume = CheckpointVolume(volume_name, checkpoint_sha256, nonce)
    if labels != _volume_labels(spec, volume):
        raise CheckpointOperationRejected(
            "Docker checkpoint volume ownership labels differ from the release"
        )
    return volume


def _volume_labels(
    spec: CheckpointImportSpec,
    volume: CheckpointVolume,
) -> dict[str, str]:
    return {
        OWNER_LABEL: OWNER_LABEL_VALUE,
        SCHEMA_LABEL: spec.volume_schema_id,
        RUNNER_LABEL: spec.release.runner_ref,
        CHECKPOINT_LABEL: volume.checkpoint_sha256,
        NONCE_LABEL: volume.operation_nonce,
    }


def _populate_volume(
    docker_binary: Path,
    helper_path: Path,
    spec: CheckpointImportSpec,
    volume: CheckpointVolume,
    release: CheckpointRelease,
    archive: Path,
) -> None:
    command = _helper_command(spec, helper_path, volume, release, "populate")
    try:
        process = subprocess.Popen(
            [str(docker_binary), "--context", DOCKER_CONTEXT, *command],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=None,
            env=_docker_environment(),
        )
    except OSError as error:
        raise CheckpointOperationRejected(
            f"Docker could not start the {spec.lane_name} checkpoint importer"
        ) from error
    try:
        assert process.stdin is not None
        with zipfile.ZipFile(archive) as bundle, bundle.open(spec.release.archive_member) as source:
            while chunk := source.read(8 * 1024 * 1024):
                process.stdin.write(chunk)
        process.stdin.close()
        status = process.wait(timeout=3600)
    except BaseException as error:
        process.kill()
        process.wait()
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise CheckpointOperationRejected(
            f"{spec.lane_name} checkpoint bytes could not be streamed to the importer"
        ) from error
    if status != 0:
        raise CheckpointOperationRejected(
            f"{spec.lane_name} checkpoint importer did not admit the streamed bytes"
        )


def _run_volume_helper(
    docker_binary: Path,
    helper_path: Path,
    spec: CheckpointImportSpec,
    volume: CheckpointVolume,
    release: CheckpointRelease | None,
    operation: str,
) -> None:
    result = _docker(
        docker_binary,
        *_helper_command(spec, helper_path, volume, release, operation),
        timeout=3600,
    )
    if result.stdout:
        raise CheckpointOperationRejected(
            f"{spec.lane_name} checkpoint volume helper produced unexpected output"
        )


def _helper_command(
    spec: CheckpointImportSpec,
    helper_path: Path,
    volume: CheckpointVolume,
    release: CheckpointRelease | None,
    operation: str,
) -> list[str]:
    _read_regular_file(helper_path, f"{spec.lane_name} checkpoint importer")
    helper_mount_source = str(helper_path)
    if any(character in helper_mount_source for character in (",", "\0", "\r", "\n")):
        raise CheckpointOperationRejected(
            f"{spec.lane_name} checkpoint importer path cannot be represented as a Docker mount"
        )
    mount = f"type=volume,src={volume.name},dst=/volume"
    if operation != "populate":
        mount += ",readonly"
    command = [
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "32",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--cpus",
        "1.0",
        "--log-driver",
        "none",
        "--mount",
        mount,
        "--mount",
        f"type=bind,src={helper_mount_source},dst=/tool/checkpoint_volume.py,readonly",
        IMPORTER_IMAGE,
        "python",
        "/tool/checkpoint_volume.py",
        operation,
    ]
    if operation == "recoverable":
        if release is not None:
            raise AssertionError("recoverable helper does not consume release facts")
        return command
    if release is None:
        raise AssertionError("checkpoint verification requires release facts")
    marker = _volume_marker(spec, volume, release)
    command.extend(
        (
            "--checkpoint-bytes",
            str(release.checkpoint_bytes),
            "--checkpoint-sha256",
            release.checkpoint_sha256,
            "--marker-base64",
            base64.b64encode(marker).decode("ascii"),
        )
    )
    return command


def _volume_marker(
    spec: CheckpointImportSpec,
    volume: CheckpointVolume,
    release: CheckpointRelease,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema_id": spec.volume_schema_id,
            "volume_name": volume.name,
            "runner_ref": spec.release.runner_ref,
            "archive_member": spec.release.archive_member,
            "checkpoint_bytes": release.checkpoint_bytes,
            "checkpoint_sha256": release.checkpoint_sha256,
            "operation_nonce": volume.operation_nonce,
        }
    )


def _remove_failed_import(
    spec: CheckpointImportSpec,
    docker_binary: Path,
    created: CheckpointVolume,
) -> None:
    current = _find_volume(spec, docker_binary, created.name)
    if current is None:
        return
    if current != created:
        raise CheckpointOperationRejected(
            "failed import volume identity changed before cleanup"
        )
    _require_unattached(docker_binary, created.name)
    result = _docker(docker_binary, "volume", "rm", created.name)
    if result.stdout.decode("utf-8", errors="strict").strip() != created.name:
        raise CheckpointOperationRejected(
            "Docker did not confirm failed import volume cleanup"
        )


def _require_unattached(docker_binary: Path, volume_name: str) -> None:
    records = _json_lines(
        _docker(
            docker_binary,
            "ps",
            "-a",
            "--filter",
            f"volume={volume_name}",
            "--format",
            "{{json .}}",
        ).stdout,
        "Docker checkpoint attachment inventory",
    )
    for record in records:
        if type(record) is not dict or type(record.get("ID")) is not str:
            raise CheckpointOperationRejected(
                "Docker checkpoint attachment inventory has an invalid record"
            )
    if records:
        raise CheckpointOperationRejected(
            "checkpoint volume is attached to a container"
        )


def _docker(
    docker_binary: Path,
    *arguments: str,
    timeout: int = 60,
) -> subprocess.CompletedProcess[bytes]:
    _require_docker_binary(docker_binary)
    try:
        result = subprocess.run(
            [str(docker_binary), "--context", DOCKER_CONTEXT, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_docker_environment(),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckpointOperationRejected(
            "Docker checkpoint operation did not complete"
        ) from error
    if len(result.stdout) > _ENGINE_OUTPUT_LIMIT or len(result.stderr) > _ENGINE_OUTPUT_LIMIT:
        raise CheckpointOperationRejected(
            "Docker checkpoint operation exceeded its diagnostic byte limit"
        )
    if result.returncode != 0:
        raise CheckpointOperationRejected(
            "Docker checkpoint operation failed without a proved state change"
        )
    return result


def _require_docker_binary(path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CheckpointOperationRejected("Docker binary does not exist") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise CheckpointOperationRejected(
            "Docker binary must resolve to one executable regular file"
        )


def _docker_environment() -> dict[str, str]:
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}


def _git(repository_root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(repository_root), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_docker_environment(),
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckpointOperationRejected(
            "checkpoint operation could not inspect committed repository bytes"
        ) from error
    if result.returncode != 0 or len(result.stdout) > _ENGINE_OUTPUT_LIMIT:
        raise CheckpointOperationRejected(
            "checkpoint operation could not prove committed repository bytes"
        )
    return result.stdout


def _single_json_document(raw: bytes, description: str) -> object:
    try:
        return json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckpointOperationRejected(
            f"{description} is not valid UTF-8 JSON"
        ) from error


def _json_lines(raw: bytes, description: str) -> list[object]:
    records: list[object] = []
    try:
        text = raw.decode("utf-8", errors="strict")
        for line in text.splitlines():
            if not line:
                raise ValueError("empty record")
            records.append(json.loads(line))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CheckpointOperationRejected(
            f"{description} is not valid JSON Lines"
        ) from error
    return records


def _validate_volume_name(spec: CheckpointImportSpec, value: object) -> None:
    pattern = re.escape(spec.volume_prefix) + r"[0-9a-f]{64}"
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        raise CheckpointOperationRejected(
            "checkpoint recovery requires one full digest-derived volume name"
        )


@contextmanager
def _volume_lock(
    spec: CheckpointImportSpec,
    volume_name: str,
    runtime_directory: Path | None,
):
    _validate_volume_name(spec, volume_name)
    directory = runtime_directory or _operator_runtime_directory()
    _require_private_runtime_directory(directory)
    lock_path = directory / f"{volume_name}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CheckpointOperationRejected(
                "checkpoint operation lock is not a same-user mode-0600 file"
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CheckpointOperationRejected(
                "checkpoint volume operation is already active"
            ) from error
        yield
    finally:
        os.close(descriptor)


def _operator_runtime_directory() -> Path:
    if os.getuid() == 0:
        directory = Path("/run/nmrpeak-repro")
        directory.mkdir(mode=0o700, exist_ok=True)
        return directory
    value = os.environ.get("XDG_RUNTIME_DIR")
    if value is None:
        return Path(f"/run/user/{os.getuid()}")
    return Path(value)


def _require_private_runtime_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CheckpointOperationRejected(
            "checkpoint runtime directory does not exist"
        ) from error
    if (
        path != resolved
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CheckpointOperationRejected(
            "checkpoint runtime directory must be a resolved same-user mode-0700 directory"
        )


def run_checkpoint_cli(
    spec: CheckpointImportSpec,
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--runner", required=True)
    import_parser.add_argument("--release", required=True)
    import_parser.add_argument("--archive", required=True, type=Path)
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--volume", required=True)
    recover_parser.add_argument("--confirm", required=True)
    arguments = parser.parse_args(argv)
    try:
        repository_root = Path(__file__).resolve().parents[1]
        if arguments.operation == "import":
            if arguments.runner != spec.release.runner_ref:
                parser.error(
                    f"--runner must be {spec.release.runner_ref}"
                )
            volume = import_checkpoint(
                spec,
                repository_root,
                arguments.archive,
                arguments.release,
            )
            print(volume.name)
        else:
            recover_checkpoint(spec, arguments.volume, arguments.confirm)
    except (CheckpointOperationRejected, OSError) as error:
        parser.exit(2, f"{spec.lane_name} checkpoint operation rejected: {error}\n")
    return 0
