"""Initialize one literal named deployment from committed examples."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory

from deployment.local_image import (
    LocalImage,
    LocalImageRejected,
    LocalImageSpec,
    resolve_local_image,
)
from nmrpeak_provider.canonical_json import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from nmrpeak_provider.provider_config import decode_provider_runtime_config
from repository_checks.chf_checkpoint import checkpoint_volume_name as chf_volume_name
from repository_checks.deployment_topology import (
    DeploymentCheckpoints,
    DeploymentImages,
    project_deployment_topology,
)
from repository_checks.hf_checkpoint import checkpoint_volume_name as hf_volume_name
from repository_checks.named_deployment import (
    RenderedGeneration,
    admit_deployment_releases,
    load_named_deployment,
    render_generation,
)
from repository_checks.nmrpeak_image_inputs import materialize_image_context
from repository_checks.nmrpeak_source import read_nmrpeak_source_revision


_DEPLOYMENT_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_TEMPLATES = {
    "provider.toml": Path("config/provider.toml.example"),
    "deployment.toml": Path("config/deployment.toml.example"),
}
_DOCKER = Path("/usr/bin/docker")
_MAX_FILE_BYTES = 262_144
_PROVIDER_ENTRYPOINT = ("python", "-m", "nmrpeak_provider.provider_main")
_HF_ENTRYPOINT = (
    "python",
    "-m",
    "models.nmrpeak_hf_v1.runner.owner_session_supervisor",
    "5",
    "5",
    "--",
    "python",
    "-m",
    "models.nmrpeak_hf_v1.runner.worker",
)
_CHF_ENTRYPOINT = (
    "python",
    "-m",
    "models.nmrpeak_chf_v1.runner.owner_session_supervisor",
    "5",
    "5",
    "--",
    "python",
    "-m",
    "models.nmrpeak_chf_v1.runner.worker",
)


class DeploymentOperationRejected(RuntimeError):
    """A host deployment operation cannot prove its narrow write boundary."""


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    """One read-only resolved Compose plan and its authenticated generation."""

    compose: dict[str, object]
    generation: RenderedGeneration
    runtime_config_id: str


def initialize_deployment(repository: Path, deployment: str) -> Path:
    """Atomically publish editable config for one new literal deployment."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    parent = root / "config/deployments"
    destination = parent / deployment
    if destination.exists() or destination.is_symlink():
        raise DeploymentOperationRejected(
            f"Deployment config already exists: {deployment}"
        )
    _require_clean_checkout(root)
    templates = {
        name: _read_committed_template(root, relative)
        for name, relative in _TEMPLATES.items()
    }
    _ensure_private_parent(parent)
    with _locked_parent(parent) as parent_fd:
        if _exists_at(parent_fd, deployment):
            raise DeploymentOperationRejected(
                f"Deployment config already exists: {deployment}"
            )
        stage_name = f".{deployment}.{secrets.token_hex(16)}.staging"
        os.mkdir(stage_name, mode=0o700, dir_fd=parent_fd)
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        written: list[str] = []
        try:
            for name, content in templates.items():
                _write_new_file(stage_fd, name, content)
                written.append(name)
            os.fsync(stage_fd)
            os.rename(
                stage_name,
                deployment,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        except BaseException:
            for name in written:
                os.unlink(name, dir_fd=stage_fd)
            os.close(stage_fd)
            stage_fd = -1
            os.rmdir(stage_name, dir_fd=parent_fd)
            raise
        finally:
            if stage_fd >= 0:
                os.close(stage_fd)
    return destination


def render_deployment_plan(
    repository: Path,
    deployment: str,
    *,
    docker: Path = _DOCKER,
) -> DeploymentPlan:
    """Resolve exact local artifacts and validate the effective Compose plan."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    _require_clean_checkout(root)
    config_root = root / "config/deployments" / deployment
    try:
        if config_root.resolve(strict=True) != config_root or not config_root.is_dir():
            raise DeploymentOperationRejected(
                "Deployment config root must be a non-symlink directory"
            )
    except OSError as error:
        raise DeploymentOperationRejected(
            f"Deployment is not initialized: {deployment}"
        ) from error
    selection = load_named_deployment(config_root / "deployment.toml")
    provider_template = _read_regular_file(config_root / "provider.toml")
    upstream_revision = read_nmrpeak_source_revision(
        root / "families/nmrpeak/source-closure.paths"
    )
    hf_declaration = _release_declaration(
        root / "models/nmrpeak_hf_v1/releases",
        selection.hf.release_name,
    )
    chf_declaration = _release_declaration(
        root / "models/nmrpeak_chf_v1/releases",
        selection.chf.release_name,
    )
    releases = admit_deployment_releases(
        selection,
        hf_release_declaration=hf_declaration,
        chf_release_declaration=chf_declaration,
        upstream_revision=upstream_revision,
    )
    revision = _committed_revision(root)
    input_ids = _image_input_ids(root, revision)
    local_images = _local_images(docker, input_ids)
    checkpoints = DeploymentCheckpoints(
        releases.hf.checkpoint_sha256,
        releases.chf.checkpoint_sha256,
    )
    images = DeploymentImages(
        local_images["provider"].image_id,
        input_ids["provider"],
        local_images["hf"].image_id,
        input_ids["hf"],
        local_images["chf"].image_id,
        input_ids["chf"],
    )
    state_root = root / "secrets/deployments" / deployment
    environment = _compose_environment(
        root,
        deployment,
        selection.provider_ref,
        images,
        checkpoints,
        provider_config=state_root / "runtime-configs/pending/provider.toml",
        frozen_generation=state_root / "generations/pending/frozen",
    )
    provisional_compose = _render_compose(root, deployment, environment, docker)
    topology = project_deployment_topology(
        provisional_compose,
        images,
        checkpoints,
    )
    generation = render_generation(
        selection,
        releases,
        provider_config_template=provider_template,
        hf_image_input_id=images.hf_input,
        chf_image_input_id=images.chf_input,
        hf_hello=_read_regular_file(
            root / "models/nmrpeak_hf_v1/provider/hello.txt"
        ),
        chf_hello=_read_regular_file(
            root / "models/nmrpeak_chf_v1/provider/hello.txt"
        ),
        topology=topology,
    )
    configured = decode_provider_runtime_config(generation.provider_config)
    if configured.endpoint.ca_file is not None:
        raise DeploymentOperationRejected(
            "Public deployment config cannot select a private Server A CA"
        )
    if configured.runner.ready_seconds > 600:
        raise DeploymentOperationRejected(
            "Runner ready timeout exceeds the Compose health start window"
        )
    runtime_config_id = _runtime_config_id(generation.provider_config)
    environment = _compose_environment(
        root,
        deployment,
        selection.provider_ref,
        images,
        checkpoints,
        provider_config=(
            state_root / "runtime-configs" / runtime_config_id / "provider.toml"
        ),
        frozen_generation=(
            state_root / "generations" / generation.frozen_generation_id / "frozen"
        ),
    )
    compose = _render_compose(root, deployment, environment, docker)
    if project_deployment_topology(compose, images, checkpoints) != topology:
        raise DeploymentOperationRejected(
            "Final retained paths changed the effective deployment topology"
        )
    return DeploymentPlan(compose, generation, runtime_config_id)


def deployment_plan_bytes(plan: DeploymentPlan) -> bytes:
    """Expose every secret-free generated artifact in one canonical preview."""

    if type(plan) is not DeploymentPlan:
        raise TypeError("Deployment preview requires one resolved plan")
    try:
        provider_config = plan.generation.provider_config.decode("utf-8")
        files = [
            {"path": frozen_file.path, "content": frozen_file.content.decode("utf-8")}
            for frozen_file in plan.generation.files
        ]
    except UnicodeDecodeError as error:
        raise DeploymentOperationRejected(
            "Generated deployment preview contains non-text public input"
        ) from error
    return canonical_json_bytes(
        {
            "schema_id": "nmrpeak.deployment_plan.v1",
            "kind": "read_only_preview",
            "frozen_generation_id": plan.generation.frozen_generation_id,
            "runtime_config_id": plan.runtime_config_id,
            "compose": plan.compose,
            "artifacts": {
                "provider.toml": provider_config,
                "frozen/manifest.json": parse_canonical_json_bytes(
                    plan.generation.manifest
                ),
                "frozen/files": files,
            },
        }
    )


def _require_deployment_name(deployment: str) -> None:
    if type(deployment) is not str or _DEPLOYMENT_NAME.fullmatch(deployment) is None:
        raise DeploymentOperationRejected(
            "Deployment name must contain only lowercase letters, digits, and interior hyphens"
        )


def _release_declaration(directory: Path, release_name: str) -> bytes:
    candidate = directory / f"{release_name}.json"
    if candidate.parent != directory:
        raise DeploymentOperationRejected("Checkpoint release name escapes its lane")
    return _read_regular_file(candidate)


def _runtime_config_id(provider_config: bytes) -> str:
    if type(provider_config) is not bytes:
        raise TypeError("Runtime config identity requires exact bytes")
    digest = sha256(b"nmrpeak.provider_runtime_config.v1\0" + provider_config)
    return f"sha256:{digest.hexdigest()}"


def _read_regular_file(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise DeploymentOperationRejected(
            f"Deployment input is unavailable: {path}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_FILE_BYTES:
            raise DeploymentOperationRejected(
                f"Deployment input must be a bounded regular file: {path}"
            )
        content = os.read(descriptor, _MAX_FILE_BYTES + 1)
        if len(content) != metadata.st_size:
            raise DeploymentOperationRejected(
                f"Deployment input changed while it was read: {path}"
            )
        return content
    finally:
        os.close(descriptor)


def _committed_revision(repository: Path) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "--verify", "HEAD"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise DeploymentOperationRejected(
            "Git could not resolve one committed deployment revision"
        )
    return revision


def _image_input_ids(repository: Path, revision: str) -> dict[str, str]:
    runners = {
        "provider": "provider",
        "hf": "nmrpeak_hf_v1",
        "chf": "nmrpeak_chf_v1",
    }
    identities: dict[str, str] = {}
    with TemporaryDirectory() as temporary:
        scratch = Path(temporary)
        for role, runner in runners.items():
            context = scratch / role
            context.mkdir()
            identities[role] = materialize_image_context(
                repository,
                revision,
                context,
                runner,
            )
    return identities


def _local_images(docker: Path, input_ids: dict[str, str]) -> dict[str, LocalImage]:
    specs = {
        "provider": LocalImageSpec(
            "numpde/nmrpeak-provider",
            input_ids["provider"],
            (("io.numpde.nmrpeak.provider.contract-id", "nmr.provider.http.v1"),),
            _PROVIDER_ENTRYPOINT,
        ),
        "hf": LocalImageSpec(
            "numpde/nmrpeak-hf-runner",
            input_ids["hf"],
            (
                ("io.numpde.nmrpeak.runner.ref", "nmrpeak_hf_v1"),
                (
                    "io.numpde.nmrpeak.runner.contract-id",
                    "nmrpeak.runner_session.hf.v1",
                ),
                ("io.numpde.nmrpeak.runner.target", "cpu-x86_64"),
            ),
            _HF_ENTRYPOINT,
        ),
        "chf": LocalImageSpec(
            "numpde/nmrpeak-chf-runner",
            input_ids["chf"],
            (
                ("io.numpde.nmrpeak.runner.ref", "nmrpeak_chf_v1"),
                (
                    "io.numpde.nmrpeak.runner.contract-id",
                    "nmrpeak.runner_session.chf.v1",
                ),
                ("io.numpde.nmrpeak.runner.target", "cpu-x86_64"),
            ),
            _CHF_ENTRYPOINT,
        ),
    }
    return {role: resolve_local_image(docker, spec) for role, spec in specs.items()}


def _compose_environment(
    repository: Path,
    deployment: str,
    provider_ref: str,
    images: DeploymentImages,
    checkpoints: DeploymentCheckpoints,
    *,
    provider_config: Path,
    frozen_generation: Path,
) -> dict[str, str]:
    state_root = repository / "secrets/deployments" / deployment
    lock_digest = sha256(
        b"nmrpeak.provider_identity_lock.v1\0" + provider_ref.encode("ascii")
    ).hexdigest()
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "DOCKER_CONTEXT": "default",
        "IMAGE_PLATFORM": "linux/amd64",
        "PROVIDER_IMAGE_REF": images.provider,
        "HF_RUNNER_IMAGE_REF": images.hf,
        "CHF_RUNNER_IMAGE_REF": images.chf,
        "PROVIDER_CONFIG_PATH": str(provider_config),
        "PROVIDER_CREDENTIAL_PATH": str(state_root / "signing.private.json"),
        "FROZEN_GENERATION_PATH": str(frozen_generation),
        "PROVIDER_IDENTITY_LOCK_VOLUME": f"nmrpeak-provider-lock-{lock_digest}",
        "PROVIDER_JOURNAL_VOLUME": f"nmrpeak-{deployment}-journal-v1",
        "HF_CHECKPOINT_VOLUME": hf_volume_name(checkpoints.hf),
        "CHF_CHECKPOINT_VOLUME": chf_volume_name(checkpoints.chf),
        "HF_CHECKPOINT_REF": checkpoints.hf,
        "CHF_CHECKPOINT_REF": checkpoints.chf,
        "HF_RUNNER_IMAGE_INPUT_ID": images.hf_input,
        "CHF_RUNNER_IMAGE_INPUT_ID": images.chf_input,
    }


def _render_compose(
    repository: Path,
    deployment: str,
    environment: dict[str, str],
    docker: Path,
) -> dict[str, object]:
    project = f"nmrpeak-{deployment}"
    try:
        result = subprocess.run(
            (
                str(docker),
                "compose",
                "--env-file",
                "/dev/null",
                "--project-name",
                project,
                "--file",
                str(repository / "compose/provider.yml"),
                "config",
                "--format",
                "json",
            ),
            cwd=repository,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentOperationRejected(
            "Docker Compose could not normalize the deployment topology"
        ) from error
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 1_048_576:
        raise DeploymentOperationRejected(
            "Docker Compose rejected the deployment topology"
        )
    try:
        document = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DeploymentOperationRejected(
            "Docker Compose returned invalid topology JSON"
        ) from error
    if type(document) is not dict:
        raise DeploymentOperationRejected(
            "Docker Compose topology must be one JSON object"
        )
    return document


def _require_clean_checkout(repository: Path) -> None:
    result = subprocess.run(
        ("git", "-C", str(repository), "status", "--short"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise DeploymentOperationRejected(
            "Git could not inspect the deployment checkout"
        )
    if result.stdout:
        raise DeploymentOperationRejected(
            "Deployment operation requires a clean committed checkout"
        )


def _read_committed_template(repository: Path, relative: Path) -> bytes:
    entry = subprocess.run(
        ("git", "-C", str(repository), "ls-tree", "HEAD", "--", str(relative)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    fields = entry.stdout.split(maxsplit=3)
    if entry.returncode != 0 or len(fields) != 4 or fields[:2] != ["100644", "blob"]:
        raise DeploymentOperationRejected(
            f"Deployment template is not a committed regular file: {relative}"
        )
    result = subprocess.run(
        ("git", "-C", str(repository), "show", f"HEAD:{relative.as_posix()}"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0 or not result.stdout:
        raise DeploymentOperationRejected(
            f"Deployment template is not a non-empty committed file: {relative}"
        )
    return result.stdout


def _ensure_private_parent(parent: Path) -> None:
    if parent.exists() or parent.is_symlink():
        if parent.is_symlink() or not parent.is_dir():
            raise DeploymentOperationRejected(
                "Deployment config parent must be a non-symlink directory"
            )
    else:
        try:
            parent.mkdir(mode=0o700)
            _fsync_directory(parent.parent)
        except FileExistsError:
            pass
    metadata = parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise DeploymentOperationRejected(
            "Deployment config parent must be operator-owned and not group/world writable"
        )


@contextmanager
def _locked_parent(parent: Path):
    descriptor = os.open(
        parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        os.close(descriptor)


def _exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _write_new_file(parent_fd: int, name: str, content: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operate one named NMRPeak deployment.")
    parser.add_argument("operation", choices=("config", "init"))
    parser.add_argument("deployment")
    options = parser.parse_args(arguments)
    repository = Path(__file__).resolve().parents[1]
    try:
        if options.operation == "init":
            initialized = initialize_deployment(repository, options.deployment)
            print(f"Initialized deployment {options.deployment}: {initialized}")
        else:
            plan = render_deployment_plan(repository, options.deployment)
            print(deployment_plan_bytes(plan).decode("utf-8"))
    except (
        DeploymentOperationRejected,
        LocalImageRejected,
        OSError,
        ValueError,
    ) as error:
        print(
            f"Cannot {options.operation} NMRPeak deployment: {error}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
    admit_deployment_releases,
