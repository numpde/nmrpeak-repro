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
import shutil
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
from deployment.provider_volumes import (
    ProviderVolumeOperationRejected,
    ensure_provider_state_volumes,
    provider_identity_lock_volume_name,
    provider_journal_volume_name,
)
from nmrpeak_provider.canonical_json import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from nmrpeak_provider.frozen_generation import frozen_generation_id
from nmrpeak_provider.provider_config import decode_provider_runtime_config
from nmrpeak_provider.provider_credential import (
    PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES,
    parse_provider_signing_credential,
)
from repository_checks.chf_checkpoint import (
    checkpoint_volume_name as chf_volume_name,
    verify_chf_checkpoint,
)
from repository_checks.deployment_topology import (
    DeploymentCheckpoints,
    DeploymentImages,
    project_deployment_topology,
)
from repository_checks.hf_checkpoint import (
    checkpoint_volume_name as hf_volume_name,
    verify_hf_checkpoint,
)
from repository_checks.named_deployment import (
    RenderedGeneration,
    admit_deployment_releases,
    load_named_deployment,
    render_generation,
)
from repository_checks.nmrpeak_image_inputs import materialize_image_context
from repository_checks.nmrpeak_source import read_nmrpeak_source_revision
from repository_checks.checkpoint import CheckpointOperationRejected


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
    releases: DeploymentReleases
    provider_ref: str
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
    return DeploymentPlan(
        compose,
        generation,
        releases,
        selection.provider_ref,
        runtime_config_id,
    )


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


def materialize_deployment_plan(
    repository: Path,
    deployment: str,
    plan: DeploymentPlan,
) -> tuple[Path, Path]:
    """Publish the plan's two immutable host inputs without engine effects."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    _validate_deployment_plan(plan)
    state_root = _ensure_deployment_state_root(root, deployment)
    with _locked_deployment_state(state_root):
        runtime_config, frozen_generation = _materialize_locked(state_root, plan)
    return runtime_config / "provider.toml", frozen_generation / "frozen"


def start_deployment(
    repository: Path,
    deployment: str,
    *,
    docker: Path = _DOCKER,
) -> DeploymentPlan:
    """Start one exact plan and prove all three durable services are ready."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    state_root = _ensure_deployment_state_root(root, deployment)
    with _locked_deployment_state(state_root):
        plan = render_deployment_plan(root, deployment, docker=docker)
        _validate_deployment_plan(plan)
        _materialize_locked(state_root, plan)
        _admit_installed_credential(state_root, plan.provider_ref)
        verify_hf_checkpoint(root, plan.releases.hf, docker_binary=docker)
        verify_chf_checkpoint(root, plan.releases.chf, docker_binary=docker)
        ensure_provider_state_volumes(
            docker,
            root,
            deployment,
            plan.provider_ref,
        )
        _inspect_project_containers(docker, root, deployment)
        _run_compose_plan(docker, root, deployment, plan)
        _require_ready_project(docker, root, deployment, plan)
        return plan


def deployment_status_bytes(
    repository: Path,
    deployment: str,
    *,
    docker: Path = _DOCKER,
) -> bytes:
    """Report the currently inspected project without consulting credentials."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    services = _inspect_project_containers(docker, root, deployment)
    records: list[dict[str, object]] = []
    for service in ("provider", "hf-runner", "chf-runner"):
        record = services.get(service)
        if record is None:
            continue
        state = record.get("State")
        image = record.get("Image")
        if type(state) is not dict or type(image) is not str:
            raise DeploymentOperationRejected(
                "Docker provider status has an invalid service record"
            )
        health = state.get("Health")
        health_status = health.get("Status") if type(health) is dict else None
        if (
            type(state.get("Status")) is not str
            or (health_status is not None and type(health_status) is not str)
        ):
            raise DeploymentOperationRejected(
                "Docker provider status has an invalid health record"
            )
        records.append(
            {
                "service": service,
                "container_id": record["Id"],
                "image_id": image,
                "state": state.get("Status"),
                "health": health_status,
            }
        )
    return canonical_json_bytes(
        {
            "schema_id": "nmrpeak.deployment_status.v1",
            "deployment": deployment,
            "project": f"nmrpeak-{deployment}",
            "services": records,
        }
    )


def stop_deployment(
    repository: Path,
    deployment: str,
    *,
    docker: Path = _DOCKER,
) -> None:
    """Remove only proved-owned project containers, network, and sessions."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    state_root = _existing_deployment_state_root(root, deployment)
    with _locked_deployment_state(state_root):
        services = _inspect_project_containers(docker, root, deployment)
        _inspect_project_resources(docker, deployment, services)
        _run_compose_down(docker, root, deployment)
        remaining = _inspect_project_containers(docker, root, deployment)
        resources = _inspect_project_resources(docker, deployment, remaining)
        if remaining or resources:
            raise DeploymentOperationRejected(
                "Provider teardown left unresolved project resources"
            )


def _validate_deployment_plan(plan: DeploymentPlan) -> None:
    if type(plan) is not DeploymentPlan:
        raise TypeError("Deployment materialization requires one resolved plan")
    if _runtime_config_id(plan.generation.provider_config) != plan.runtime_config_id:
        raise DeploymentOperationRejected(
            "Deployment runtime config identity does not match its bytes"
        )
    if frozen_generation_id(plan.generation.manifest) != (
        plan.generation.frozen_generation_id
    ):
        raise DeploymentOperationRejected(
            "Deployment frozen generation identity does not match its manifest"
        )


def _materialize_locked(
    state_root: Path,
    plan: DeploymentPlan,
) -> tuple[Path, Path]:
    runtime_root = _ensure_private_directory(state_root / "runtime-configs")
    generation_root = _ensure_private_directory(state_root / "generations")
    runtime_config = _publish_retained_tree(
        runtime_root,
        plan.runtime_config_id,
        {"provider.toml": plan.generation.provider_config},
    )
    frozen_files = {"frozen/manifest.json": plan.generation.manifest}
    frozen_files.update(
        {
            f"frozen/{frozen_file.path}": frozen_file.content
            for frozen_file in plan.generation.files
        }
    )
    frozen_generation = _publish_retained_tree(
        generation_root,
        plan.generation.frozen_generation_id,
        frozen_files,
    )
    return runtime_config, frozen_generation


def _admit_installed_credential(state_root: Path, provider_ref: str) -> None:
    path = state_root / "signing.private.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise DeploymentOperationRejected(
            "Provider signing credential is not installed"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES
        ):
            raise DeploymentOperationRejected(
                "Provider signing credential must be operator-owned mode 0600"
            )
        raw = os.read(descriptor, PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES + 1)
        if len(raw) != metadata.st_size:
            raise DeploymentOperationRejected(
                "Provider signing credential changed while it was read"
            )
    finally:
        os.close(descriptor)
    try:
        credential = parse_provider_signing_credential(raw)
    except ValueError as error:
        raise DeploymentOperationRejected(
            "Provider signing credential is invalid"
        ) from error
    if credential.provider_ref != provider_ref:
        raise DeploymentOperationRejected(
            "Provider signing credential belongs to another provider"
        )


def _run_compose_plan(
    docker: Path,
    repository: Path,
    deployment: str,
    plan: DeploymentPlan,
) -> None:
    try:
        with TemporaryDirectory() as temporary:
            compose_path = Path(temporary) / "compose.json"
            compose_path.write_bytes(canonical_json_bytes(plan.compose))
            _docker_command(
                docker,
                (
                    "compose",
                    "--env-file",
                    "/dev/null",
                    "--project-directory",
                    str(repository),
                    "--project-name",
                    f"nmrpeak-{deployment}",
                    "--file",
                    str(compose_path),
                    "up",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "660",
                    "--no-build",
                    "--pull",
                    "never",
                ),
                timeout=720,
            )
    except DeploymentOperationRejected as error:
        raise DeploymentOperationRejected(
            "Docker Compose startup was rejected; runtime state may be partial"
        ) from error


def _run_compose_down(
    docker: Path,
    repository: Path,
    deployment: str,
) -> None:
    try:
        teardown = _read_committed_template(
            repository,
            Path("compose/provider-teardown.yml"),
        )
        with TemporaryDirectory() as temporary:
            compose = Path(temporary) / "provider-teardown.yml"
            compose.write_bytes(teardown)
            prefix = (
                "compose",
                "--env-file",
                "/dev/null",
                "--project-directory",
                str(repository),
                "--project-name",
                f"nmrpeak-{deployment}",
                "--file",
                str(compose),
            )
            _docker_command(
                docker,
                (*prefix, "stop", "--timeout", "600", "provider"),
                timeout=660,
            )
            _docker_command(
                docker,
                (
                    *prefix,
                    "stop",
                    "--timeout",
                    "20",
                    "hf-runner",
                    "chf-runner",
                ),
                timeout=80,
            )
            _docker_command(
                docker,
                (
                    *prefix,
                    "down",
                    "--timeout",
                    "20",
                    "--remove-orphans",
                    "--volumes",
                ),
                timeout=80,
            )
    except DeploymentOperationRejected as error:
        raise DeploymentOperationRejected(
            "Docker Compose teardown was rejected; runtime state may be partial"
        ) from error


def _inspect_project_containers(
    docker: Path,
    repository: Path,
    deployment: str,
) -> dict[str, dict[str, object]]:
    project = f"nmrpeak-{deployment}"
    inventory = _docker_command(
        docker,
        (
            "ps",
            "--all",
            "--no-trunc",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ),
        timeout=60,
    ).stdout.decode("ascii", errors="strict").splitlines()
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in inventory):
        raise DeploymentOperationRejected(
            "Docker returned malformed provider project container identities"
        )
    if not inventory:
        return {}
    document = _json_output(
        _docker_command(
            docker,
            ("inspect", *inventory),
            timeout=60,
        ).stdout,
        "Docker provider project inspection",
    )
    if type(document) is not list or len(document) != len(inventory):
        raise DeploymentOperationRejected(
            "Docker provider project inspection has an invalid shape"
        )
    services: dict[str, dict[str, object]] = {}
    for record in document:
        if type(record) is not dict:
            raise DeploymentOperationRejected(
                "Docker provider project inspection has an invalid record"
            )
        config = record.get("Config")
        labels = config.get("Labels") if type(config) is dict else None
        service = labels.get("com.docker.compose.service") if type(labels) is dict else None
        if (
            record.get("Id") not in inventory
            or type(service) is not str
            or service not in {"provider", "hf-runner", "chf-runner"}
            or service in services
            or labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.oneoff") != "False"
            or labels.get("com.docker.compose.project.working_dir") != str(repository)
        ):
            raise DeploymentOperationRejected(
                "Provider project contains a foreign or ambiguous container"
            )
        services[service] = record
    return services


def _inspect_project_resources(
    docker: Path,
    deployment: str,
    services: dict[str, dict[str, object]],
) -> tuple[str, ...]:
    project = f"nmrpeak-{deployment}"
    project_container_ids = {record["Id"] for record in services.values()}
    expected_volumes = {
        f"{project}_hf-session": "hf-session",
        f"{project}_chf-session": "chf-session",
    }
    volume_names = _docker_command(
        docker,
        (
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ),
        timeout=60,
    ).stdout.decode("ascii", errors="strict").splitlines()
    if len(set(volume_names)) != len(volume_names) or any(
        name not in expected_volumes for name in volume_names
    ):
        raise DeploymentOperationRejected(
            "Provider project contains a foreign or ambiguous session volume"
        )
    if volume_names:
        document = _json_output(
            _docker_command(
                docker,
                ("volume", "inspect", *volume_names),
                timeout=60,
            ).stdout,
            "Docker provider session volume inspection",
        )
        if type(document) is not list or len(document) != len(volume_names):
            raise DeploymentOperationRejected(
                "Docker provider session volume inspection has an invalid shape"
            )
        for record in document:
            if type(record) is not dict:
                raise DeploymentOperationRejected(
                    "Docker provider session volume inspection has an invalid record"
                )
            name = record.get("Name")
            labels = record.get("Labels")
            if (
                name not in volume_names
                or type(labels) is not dict
                or record.get("Driver") != "local"
                or record.get("Options")
                != {
                    "device": "tmpfs",
                    "o": "size=1m,uid=65532,gid=65532,mode=0700",
                    "type": "tmpfs",
                }
                or labels.get("com.docker.compose.project") != project
                or labels.get("com.docker.compose.volume")
                != expected_volumes[name]
            ):
                raise DeploymentOperationRejected(
                    "Provider project session volume ownership is invalid"
                )
            attachments = _docker_command(
                docker,
                (
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--quiet",
                    "--filter",
                    f"volume={name}",
                ),
                timeout=60,
            ).stdout.decode("ascii", errors="strict").splitlines()
            if any(
                re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in attachments
            ) or not set(attachments).issubset(project_container_ids):
                raise DeploymentOperationRejected(
                    "Provider project session volume has a foreign attachment"
                )

    network_ids = _docker_command(
        docker,
        (
            "network",
            "ls",
            "--no-trunc",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ),
        timeout=60,
    ).stdout.decode("ascii", errors="strict").splitlines()
    if len(set(network_ids)) != len(network_ids) or any(
        re.fullmatch(r"[0-9a-f]{64}", value) is None for value in network_ids
    ):
        raise DeploymentOperationRejected(
            "Docker returned malformed provider project network identities"
        )
    network_names: list[str] = []
    if network_ids:
        document = _json_output(
            _docker_command(
                docker,
                ("network", "inspect", *network_ids),
                timeout=60,
            ).stdout,
            "Docker provider network inspection",
        )
        if type(document) is not list or len(document) != len(network_ids):
            raise DeploymentOperationRejected(
                "Docker provider network inspection has an invalid shape"
            )
        for record in document:
            if type(record) is not dict:
                raise DeploymentOperationRejected(
                    "Docker provider network inspection has an invalid record"
                )
            labels = record.get("Labels")
            containers = record.get("Containers")
            name = record.get("Name")
            if (
                record.get("Id") not in network_ids
                or name != f"{project}_default"
                or network_names
                or record.get("Driver") != "bridge"
                or type(labels) is not dict
                or labels.get("com.docker.compose.project") != project
                or labels.get("com.docker.compose.network") != "default"
                or type(containers) is not dict
                or not set(containers).issubset(project_container_ids)
            ):
                raise DeploymentOperationRejected(
                    "Provider project network ownership is invalid"
                )
            network_names.append(name)
    return tuple(volume_names + network_names)


def _require_ready_project(
    docker: Path,
    repository: Path,
    deployment: str,
    plan: DeploymentPlan,
) -> None:
    services = _inspect_project_containers(docker, repository, deployment)
    expected_services = plan.compose["services"]
    if set(services) != {"provider", "hf-runner", "chf-runner"}:
        raise DeploymentOperationRejected(
            "Provider startup did not leave exactly three durable services"
        )
    for service, record in services.items():
        expected = expected_services[service]
        state = record.get("State")
        if (
            type(expected) is not dict
            or record.get("Image") != expected.get("image")
            or type(state) is not dict
            or state.get("Status") != "running"
        ):
            raise DeploymentOperationRejected(
                f"Provider startup did not prove the selected {service} image running"
            )
        if service == "provider":
            health = state.get("Health")
            if type(health) is not dict or health.get("Status") != "healthy":
                raise DeploymentOperationRejected(
                    "Provider startup did not prove provider readiness"
                )


def _docker_command(
    docker: Path,
    arguments: tuple[str, ...],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            (str(docker), "--context", "default", *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                "DOCKER_CONTEXT": "default",
            },
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentOperationRejected(
            "Docker provider deployment operation did not complete"
        ) from error
    if (
        result.returncode != 0
        or len(result.stdout) > 1_048_576
        or len(result.stderr) > 1_048_576
    ):
        raise DeploymentOperationRejected(
            "Docker provider deployment operation was rejected"
        )
    return result


def _json_output(raw: bytes, operation: str) -> object:
    try:
        return json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DeploymentOperationRejected(f"{operation} returned invalid JSON") from error


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


def _ensure_deployment_state_root(repository: Path, deployment: str) -> Path:
    secrets_root = _ensure_private_directory(repository / "secrets")
    deployments = _ensure_private_directory(secrets_root / "deployments")
    return _ensure_private_directory(deployments / deployment)


def _existing_deployment_state_root(repository: Path, deployment: str) -> Path:
    path = repository / "secrets/deployments" / deployment
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise DeploymentOperationRejected(
            f"Deployment state is not initialized: {deployment}"
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DeploymentOperationRejected(
            f"Deployment state directory must be operator-owned mode 0700: {path}"
        )
    return path


def _ensure_private_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700)
        _fsync_directory(path.parent)
    except FileExistsError:
        pass
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise DeploymentOperationRejected(
            f"Deployment state directory is unavailable: {path}"
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DeploymentOperationRejected(
            f"Deployment state directory must be operator-owned mode 0700: {path}"
        )
    return path


@contextmanager
def _locked_deployment_state(state_root: Path):
    lock_path = state_root / ".lifecycle.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise DeploymentOperationRejected(
                "Deployment lifecycle lock must be operator-owned mode 0600"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _publish_retained_tree(
    parent: Path,
    identity: str,
    files: dict[str, bytes],
) -> Path:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is None:
        raise DeploymentOperationRejected("Retained input identity is malformed")
    destination = parent / identity
    if destination.exists() or destination.is_symlink():
        _require_retained_tree(destination, files)
        return destination
    stage = parent / f".{identity}.{secrets.token_hex(16)}.staging"
    stage.mkdir(mode=0o700)
    try:
        for relative, content in files.items():
            candidate = Path(relative)
            if (
                candidate.is_absolute()
                or not candidate.parts
                or any(part in {"", ".", ".."} for part in candidate.parts)
                or type(content) is not bytes
            ):
                raise DeploymentOperationRejected(
                    "Retained deployment input inventory is invalid"
                )
            directory = stage
            for part in candidate.parts[:-1]:
                directory = directory / part
                if not directory.exists():
                    directory.mkdir(mode=0o700)
            descriptor = os.open(
                stage / candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o400,
            )
            try:
                if os.write(descriptor, content) != len(content):
                    raise OSError("Retained deployment input write was incomplete")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _fsync_tree(stage)
        os.rename(stage, destination)
        _fsync_directory(parent)
    except BaseException:
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage)
        raise
    _require_retained_tree(destination, files)
    return destination


def _require_retained_tree(path: Path, expected: dict[str, bytes]) -> None:
    if path.is_symlink() or not path.is_dir():
        raise DeploymentOperationRejected(
            f"Retained deployment input is not a directory: {path.name}"
        )
    actual_files: dict[str, bytes] = {}
    actual_directories: set[str] = set()
    for directory, names, filenames in os.walk(path, followlinks=False):
        current = Path(directory)
        metadata = current.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise DeploymentOperationRejected(
                f"Retained deployment directory posture has drifted: {path.name}"
            )
        relative_directory = current.relative_to(path).as_posix()
        if relative_directory != ".":
            actual_directories.add(relative_directory)
        for name in names:
            child = current / name
            if child.is_symlink():
                raise DeploymentOperationRejected(
                    f"Retained deployment directory contains a symlink: {path.name}"
                )
        for name in filenames:
            child = current / name
            try:
                descriptor = os.open(
                    child,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                )
            except OSError as error:
                raise DeploymentOperationRejected(
                    f"Retained deployment file is unavailable: {path.name}"
                ) from error
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                ):
                    raise DeploymentOperationRejected(
                        f"Retained deployment file posture has drifted: {path.name}"
                    )
                actual_files[(child.relative_to(path)).as_posix()] = os.read(
                    descriptor,
                    _MAX_FILE_BYTES + 1,
                )
            finally:
                os.close(descriptor)
    expected_directories = {
        Path(*Path(relative).parts[:index]).as_posix()
        for relative in expected
        for index in range(1, len(Path(relative).parts))
    }
    if actual_files != expected or actual_directories != expected_directories:
        raise DeploymentOperationRejected(
            f"Retained deployment input bytes have drifted: {path.name}"
        )


def _fsync_tree(root: Path) -> None:
    directories = [Path(directory) for directory, _, _ in os.walk(root)]
    for directory in reversed(directories):
        _fsync_directory(directory)


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
        "PROVIDER_IDENTITY_LOCK_VOLUME": provider_identity_lock_volume_name(
            provider_ref
        ),
        "PROVIDER_JOURNAL_VOLUME": provider_journal_volume_name(deployment),
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
    parser.add_argument("operation", choices=("config", "down", "init", "status", "up"))
    parser.add_argument("deployment")
    options = parser.parse_args(arguments)
    repository = Path(__file__).resolve().parents[1]
    try:
        if options.operation == "init":
            initialized = initialize_deployment(repository, options.deployment)
            print(f"Initialized deployment {options.deployment}: {initialized}")
        elif options.operation == "config":
            plan = render_deployment_plan(repository, options.deployment)
            print(deployment_plan_bytes(plan).decode("utf-8"))
        elif options.operation == "up":
            plan = start_deployment(repository, options.deployment)
            print(
                "Provider deployment ready: "
                f"{options.deployment} {plan.generation.frozen_generation_id}"
            )
        elif options.operation == "status":
            print(deployment_status_bytes(repository, options.deployment).decode("utf-8"))
        else:
            stop_deployment(repository, options.deployment)
            print(
                f"Provider deployment stopped: {options.deployment}. "
                "Containers, its project network, and session volumes were removed; "
                "credentials, journal, checkpoints, retained inputs, and images remain."
            )
    except (
        CheckpointOperationRejected,
        DeploymentOperationRejected,
        LocalImageRejected,
        OSError,
        ProviderVolumeOperationRejected,
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
