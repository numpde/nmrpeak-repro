"""Own the two external Docker volumes created for one provider deployment."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from repository_checks.checkpoint import (
    DOCKER_CONTEXT,
    IMPORTER_IMAGE,
    OWNER_LABEL,
    OWNER_LABEL_VALUE,
    SCHEMA_LABEL,
)


_DEPLOYMENT_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_PROVIDER_REF = re.compile(r"provider:[A-Za-z0-9_.-]{1,119}")
_PROVIDER_LABEL = "io.github.numpde.nmrpeak.provider"
_DEPLOYMENT_LABEL = "io.github.numpde.nmrpeak.deployment"
_LOCK_SCHEMA = "nmrpeak.provider_identity_lock_volume.v1"
_JOURNAL_SCHEMA = "nmrpeak.provider_journal_volume.v1"
_HELPER = Path("docker/provider_volume.py")
_OUTPUT_LIMIT = 1_048_576


class ProviderVolumeOperationRejected(RuntimeError):
    """Provider volume ownership or initialization could not be proved."""


@dataclass(frozen=True, slots=True)
class ProviderStateVolumes:
    """The exact provider-owned external volume names for one deployment."""

    identity_lock: str
    journal: str


@dataclass(frozen=True, slots=True)
class _VolumeSpec:
    name: str
    labels: dict[str, str]
    helper_operation: str
    helper_argument: str | None


def provider_identity_lock_volume_name(provider_ref: str) -> str:
    if type(provider_ref) is not str or _PROVIDER_REF.fullmatch(provider_ref) is None:
        raise ProviderVolumeOperationRejected(
            "Provider lock volume requires one valid provider reference"
        )
    digest = sha256(
        b"nmrpeak.provider_identity_lock.v1\0" + provider_ref.encode("ascii")
    ).hexdigest()
    return f"nmrpeak-provider-lock-{digest}"


def provider_journal_volume_name(deployment: str) -> str:
    if type(deployment) is not str or _DEPLOYMENT_NAME.fullmatch(deployment) is None:
        raise ProviderVolumeOperationRejected(
            "Provider journal volume requires one valid deployment name"
        )
    return f"nmrpeak-{deployment}-journal-v1"


def ensure_provider_state_volumes(
    docker: Path,
    repository: Path,
    deployment: str,
    provider_ref: str,
) -> ProviderStateVolumes:
    """Create or reprove the lock and journal volumes, then admit their roots."""

    helper = _committed_helper_path(repository)
    lock_name = provider_identity_lock_volume_name(provider_ref)
    journal_name = provider_journal_volume_name(deployment)
    common = {
        OWNER_LABEL: OWNER_LABEL_VALUE,
        _PROVIDER_LABEL: provider_ref,
    }
    specs = (
        _VolumeSpec(
            lock_name,
            common | {SCHEMA_LABEL: _LOCK_SCHEMA},
            "identity-lock",
            provider_ref,
        ),
        _VolumeSpec(
            journal_name,
            common
            | {
                SCHEMA_LABEL: _JOURNAL_SCHEMA,
                _DEPLOYMENT_LABEL: deployment,
            },
            "journal",
            None,
        ),
    )
    for spec in specs:
        _ensure_volume(docker, spec)
        _admit_volume_root(docker, helper, spec)
    return ProviderStateVolumes(lock_name, journal_name)


def _ensure_volume(docker: Path, spec: _VolumeSpec) -> None:
    records = _json_lines(
        _docker(
            docker,
            "volume",
            "ls",
            "--filter",
            f"name=^{spec.name}$",
            "--format",
            "{{json .}}",
        ).stdout,
        "Docker provider volume inventory",
    )
    names = [record.get("Name") for record in records if type(record) is dict]
    if len(names) != len(records) or any(type(name) is not str for name in names):
        raise ProviderVolumeOperationRejected(
            "Docker provider volume inventory has an invalid shape"
        )
    if not names:
        arguments = ["volume", "create", "--driver", "local"]
        for name, value in sorted(spec.labels.items()):
            arguments.extend(("--label", f"{name}={value}"))
        arguments.append(spec.name)
        created = _docker(docker, *arguments).stdout.decode("utf-8").strip()
        if created != spec.name:
            raise ProviderVolumeOperationRejected(
                "Docker did not confirm the exact created provider volume"
            )
    elif names != [spec.name]:
        raise ProviderVolumeOperationRejected(
            "Docker provider volume inventory is ambiguous"
        )
    document = _json_document(
        _docker(docker, "volume", "inspect", spec.name).stdout,
        "Docker provider volume inspection",
    )
    if (
        type(document) is not list
        or len(document) != 1
        or type(document[0]) is not dict
        or document[0].get("Name") != spec.name
        or document[0].get("Driver") != "local"
        or document[0].get("Labels") != spec.labels
    ):
        raise ProviderVolumeOperationRejected(
            f"Docker provider volume ownership has drifted: {spec.name}"
        )


def _admit_volume_root(docker: Path, helper: Path, spec: _VolumeSpec) -> None:
    helper_source = str(helper)
    if any(character in helper_source for character in (",", "\0", "\r", "\n")):
        raise ProviderVolumeOperationRejected(
            "Provider volume helper path cannot be represented as a Docker mount"
        )
    arguments = [
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
    ]
    if spec.helper_operation == "journal":
        arguments.extend(("--cap-add", "CHOWN"))
    arguments.extend(
        (
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "32",
            "--memory",
            "128m",
            "--memory-swap",
            "128m",
            "--cpus",
            "1.0",
            "--log-driver",
            "none",
            "--mount",
            f"type=volume,src={spec.name},dst=/volume",
            "--mount",
            f"type=bind,src={helper_source},dst=/tool/provider_volume.py,readonly",
            IMPORTER_IMAGE,
            "python",
            "/tool/provider_volume.py",
            spec.helper_operation,
        )
    )
    if spec.helper_argument is not None:
        arguments.append(spec.helper_argument)
    result = _docker(docker, *arguments)
    if result.stdout:
        raise ProviderVolumeOperationRejected(
            "Provider volume helper produced unexpected output"
        )


def _committed_helper_path(repository: Path) -> Path:
    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise ProviderVolumeOperationRejected(
            "Provider volume repository must be one resolved directory"
        )
    helper = root / _HELPER
    try:
        metadata = helper.stat(follow_symlinks=False)
        content = helper.read_bytes()
    except OSError as error:
        raise ProviderVolumeOperationRejected(
            "Provider volume helper is unavailable"
        ) from error
    committed = subprocess.run(
        ("git", "-C", str(root), "show", f"HEAD:{_HELPER.as_posix()}"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or committed.returncode != 0
        or not content
        or content != committed.stdout
    ):
        raise ProviderVolumeOperationRejected(
            "Provider volume helper must equal its committed regular file"
        )
    return helper


def _docker(docker: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            (str(docker), "--context", DOCKER_CONTEXT, *arguments),
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                "DOCKER_CONTEXT": DOCKER_CONTEXT,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProviderVolumeOperationRejected(
            "Docker provider volume operation did not complete"
        ) from error
    if (
        result.returncode != 0
        or len(result.stdout) > _OUTPUT_LIMIT
        or len(result.stderr) > _OUTPUT_LIMIT
    ):
        raise ProviderVolumeOperationRejected(
            "Docker provider volume operation was rejected"
        )
    return result


def _json_lines(raw: bytes, operation: str) -> list[object]:
    try:
        return [json.loads(line) for line in raw.splitlines() if line]
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProviderVolumeOperationRejected(f"{operation} returned invalid JSON") from error


def _json_document(raw: bytes, operation: str) -> object:
    try:
        return json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProviderVolumeOperationRejected(f"{operation} returned invalid JSON") from error
