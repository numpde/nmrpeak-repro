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
import select
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
    inspect_provider_identity_lock_volume,
    inspect_provider_journal_volume,
    provider_identity_lock_volume_name,
    provider_journal_volume_name,
    remove_provider_journal_volume,
)
from nmrpeak_provider.attempt_inventory import (
    AttemptInventory,
    AttemptInventoryReadFailed,
    AttemptInventoryRejected,
    read_attempt_inventory,
)
from nmrpeak_provider.canonical_json import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from nmrpeak_provider.attempt_journal import validate_frozen_generation_id
from nmrpeak_provider.frozen_generation import (
    frozen_generation_id,
    load_frozen_generation,
)
from nmrpeak_provider.provider_config import (
    ProviderEndpointConfig,
    ProviderRuntimeConfig,
    decode_provider_runtime_config,
    server_a_authority_id,
)
from nmrpeak_provider.provider_api import ProviderApiClient
from nmrpeak_provider.provider_credential import (
    PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES,
    ProviderSigningCredential,
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
_SETFACL = Path("/usr/bin/setfacl")
_GETFACL = Path("/usr/bin/getfacl")
_MAX_FILE_BYTES = 262_144
_PRIVATE_DIRECTORY_ACL = ("user::rwx", "group::---", "other::---")
_PROVIDER_DIRECTORY_ACL = (
    "user::rwx",
    "user:65532:r-x",
    "group::---",
    "mask::r-x",
    "other::---",
)
_PRIVATE_READONLY_FILE_ACL = ("user::r--", "group::---", "other::---")
_PRIVATE_WRITABLE_FILE_ACL = ("user::rw-", "group::---", "other::---")
_PROVIDER_READONLY_FILE_ACL = (
    "user::r--",
    "user:65532:r--",
    "group::---",
    "mask::r--",
    "other::---",
)
_PROVIDER_WRITABLE_FILE_ACL = (
    "user::rw-",
    "user:65532:r--",
    "group::---",
    "mask::r--",
    "other::---",
)
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
    localhost_ca_certificate: Path | None = None,
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
    localhost_ca = _admit_localhost_ca_certificate(localhost_ca_certificate)
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
    _admit_server_a_endpoint(
        decode_provider_runtime_config(provider_template).endpoint,
        localhost=localhost_ca is not None,
    )
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
        localhost_ca_certificate=localhost_ca,
    )
    provisional_compose = _render_compose(
        root,
        deployment,
        environment,
        docker,
        localhost=localhost_ca is not None,
    )
    topology = project_deployment_topology(
        provisional_compose,
        images,
        checkpoints,
        private_ca=localhost_ca is not None,
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
    _admit_server_a_endpoint(
        configured.endpoint,
        localhost=localhost_ca is not None,
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
            state_root
            / "runtime-configs"
            / _retained_identity_name(runtime_config_id)
            / "provider.toml"
        ),
        frozen_generation=(
            state_root
            / "generations"
            / _retained_identity_name(generation.frozen_generation_id)
            / "frozen"
        ),
        localhost_ca_certificate=localhost_ca,
    )
    compose = _render_compose(
        root,
        deployment,
        environment,
        docker,
        localhost=localhost_ca is not None,
    )
    if project_deployment_topology(
        compose,
        images,
        checkpoints,
        private_ca=localhost_ca is not None,
    ) != topology:
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
    localhost_ca_certificate: Path | None = None,
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
        plan = render_deployment_plan(
            root,
            deployment,
            localhost_ca_certificate=localhost_ca_certificate,
            docker=docker,
        )
        _validate_deployment_plan(plan)
        _materialize_locked(state_root, plan)
        _admit_installed_credential(state_root, plan.provider_ref)
        _admit_interpreter_configs(state_root)
        verify_hf_checkpoint(root, plan.releases.hf, docker_binary=docker)
        verify_chf_checkpoint(root, plan.releases.chf, docker_binary=docker)
        ensure_provider_state_volumes(
            docker,
            root,
            deployment,
            plan.provider_ref,
            server_a_authority_id(
                decode_provider_runtime_config(plan.generation.provider_config).endpoint
            ),
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


def install_provider_credential(
    repository: Path,
    deployment: str,
    nmr_api_v1: Path,
    *,
    replace: bool = False,
    docker: Path = _DOCKER,
) -> Path:
    """Install the one API-issued credential matching a named deployment."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    config_root = root / "config/deployments" / deployment
    try:
        if config_root.resolve(strict=True) != config_root or not config_root.is_dir():
            raise DeploymentOperationRejected(
                f"Deployment is not initialized: {deployment}"
            )
    except OSError as error:
        raise DeploymentOperationRejected(
            f"Deployment is not initialized: {deployment}"
        ) from error
    provider_ref = load_named_deployment(
        config_root / "deployment.toml"
    ).provider_ref
    credential_bytes, _ = _select_api_credential(
        nmr_api_v1,
        provider_ref,
    )
    state_root = _ensure_deployment_state_root(root, deployment)
    destination = state_root / "signing.private.json"
    with _locked_deployment_state(state_root):
        if destination.exists() or destination.is_symlink():
            installed_bytes, installed = _read_owned_private_credential(
                destination,
                "Installed provider signing credential",
            )
            if installed.provider_ref != provider_ref:
                raise DeploymentOperationRejected(
                    "Installed provider signing credential belongs to another provider"
                )
            if installed_bytes == credential_bytes:
                _grant_provider_file_access(destination, owner_write=True)
                return destination
            if not replace:
                raise DeploymentOperationRejected(
                    "Deployment already has a different provider signing credential; "
                    "set REPLACE=1 to replace it"
                )
            _require_stopped_project(docker, root, deployment)
        _publish_private_credential(
            destination,
            credential_bytes,
            replace=destination.exists(),
        )
    return destination


def show_provider_logs(
    repository: Path,
    deployment: str,
    *,
    docker: Path = _DOCKER,
) -> None:
    """Hand the terminal to the exact provider container's bounded log tail."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    provider = _inspect_project_containers(docker, root, deployment).get("provider")
    if provider is None:
        raise DeploymentOperationRejected(
            f"Deployment has no provider container: {deployment}"
        )
    state = provider.get("State")
    if type(state) is not dict or type(state.get("Running")) is not bool:
        raise DeploymentOperationRejected(
            "Docker provider log selection has an invalid state record"
        )
    arguments = [
        str(docker),
        "--context",
        "default",
        "logs",
        "--timestamps",
        "--tail",
        "200",
    ]
    if state["Running"]:
        arguments.append("--follow")
    arguments.append(provider["Id"])
    os.execve(
        str(docker),
        arguments,
        {
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp",
            "DOCKER_CONTEXT": "default",
        },
    )


def remove_frozen_generation(
    repository: Path,
    deployment: str,
    frozen_generation: str,
    confirmation: str,
    *,
    docker: Path = _DOCKER,
) -> None:
    """Remove one stopped deployment generation absent from its journal."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    try:
        validate_frozen_generation_id(frozen_generation)
    except (TypeError, ValueError) as error:
        raise DeploymentOperationRejected(
            "Frozen generation removal requires one full SHA-256 identity"
        ) from error
    if confirmation != frozen_generation:
        raise DeploymentOperationRejected(
            "Frozen generation removal confirmation must equal its full identity"
        )
    state_root = _existing_deployment_state_root(root, deployment)
    with _locked_deployment_state(state_root):
        services = _inspect_project_containers(docker, root, deployment)
        _require_stopped_services(services, "Frozen generation removal")
        provider_ref = load_named_deployment(
            root / "config/deployments" / deployment / "deployment.toml"
        ).provider_ref
        references = _journal_generation_ids(
            docker,
            root,
            deployment,
            provider_ref,
            server_a_authority_id(
                _runtime_config(root, deployment).endpoint
            ),
            services,
        )
        if frozen_generation in references:
            raise DeploymentOperationRejected(
                "Provider journal still references the frozen generation"
            )
        _remove_retained_generation(state_root, frozen_generation)


def retire_provider_journal(
    repository: Path,
    deployment: str,
    confirmation: str,
    *,
    docker: Path = _DOCKER,
) -> str:
    """Delete one stopped provider journal after Server A proves it has no live Attempts."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    journal_name = provider_journal_volume_name(deployment)
    if confirmation != journal_name:
        raise DeploymentOperationRejected(
            "Journal retirement confirmation must equal the full volume name"
        )
    state_root = _existing_deployment_state_root(root, deployment)
    with _locked_deployment_state(state_root):
        services = _inspect_project_containers(docker, root, deployment)
        _require_stopped_services(services, "Provider journal retirement")
        selection = load_named_deployment(
            root / "config/deployments" / deployment / "deployment.toml"
        )
        configured = _journal_runtime_config(root, deployment)
        authority_id = server_a_authority_id(configured.endpoint)
        _, credential = _read_owned_private_credential(
            state_root / "signing.private.json",
            "Provider journal retirement credential",
        )
        if credential.provider_ref != selection.provider_ref:
            raise DeploymentOperationRejected(
                "Provider journal retirement credential belongs to another provider"
            )
        admitted_journal, attachments = inspect_provider_journal_volume(
            docker,
            deployment,
            selection.provider_ref,
            authority_id,
        )
        if admitted_journal != journal_name or attachments:
            raise DeploymentOperationRejected(
                "Provider journal retirement requires one unattached exact volume"
            )
        lock_volume = inspect_provider_identity_lock_volume(
            docker,
            selection.provider_ref,
        )
        provider_image = _resolve_provider_image(docker, root)
        with _held_provider_identity_lock(
            docker,
            lock_volume,
            selection.provider_ref,
            provider_image,
        ):
            api = ProviderApiClient(
                configured.endpoint.materialize(),
                credential.credential_ref,
                credential.private_key,
            )
            try:
                inventory = read_attempt_inventory(
                    api=api,
                    maximum_pages=configured.process.inventory_maximum_pages,
                )
            except AttemptInventoryRejected as error:
                raise DeploymentOperationRejected(
                    "Provider journal retirement could not prove a complete Attempt inventory"
                ) from error
            if type(inventory) is AttemptInventoryReadFailed:
                raise DeploymentOperationRejected(
                    "Provider journal retirement could not read a complete Attempt inventory"
                )
            if type(inventory) is not AttemptInventory:
                raise DeploymentOperationRejected(
                    "Provider journal retirement received an invalid Attempt inventory"
                )
            if inventory.attempts:
                raise DeploymentOperationRejected(
                    "Provider journal retirement found in-progress Attempts"
                )
            return remove_provider_journal_volume(
                docker,
                deployment,
                selection.provider_ref,
                authority_id,
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
    _, credential = _read_owned_private_credential(
        path,
        "Provider signing credential",
    )
    if credential.provider_ref != provider_ref:
        raise DeploymentOperationRejected(
            "Provider signing credential belongs to another provider"
        )
    _grant_provider_file_access(path, owner_write=True)
    _read_owned_private_credential(
        path,
        "Provider signing credential",
        require_provider_access=True,
    )


def _admit_interpreter_configs(state_root: Path) -> None:
    root = state_root / "openai-chat-completions.d"
    try:
        metadata = root.stat(follow_symlinks=False)
        entries = tuple(root.iterdir())
    except OSError as error:
        raise DeploymentOperationRejected(
            "Interpreter endpoint configuration is unavailable"
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or not 1 <= len(entries) <= 4
    ):
        raise DeploymentOperationRejected(
            "Interpreter endpoint configuration must contain one to four private files"
        )
    for path in entries:
        try:
            entry = path.stat(follow_symlinks=False)
        except OSError as error:
            raise DeploymentOperationRejected(
                "Interpreter endpoint configuration contains an unreadable entry"
            ) from error
        if (
            path.suffix != ".toml"
            or not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or entry.st_size > 64 * 1024
        ):
            raise DeploymentOperationRejected(
                "Interpreter endpoint configuration contains an invalid file"
            )
        access = _read_acl(path, "Interpreter endpoint configuration")
        if access not in {
            _PRIVATE_WRITABLE_FILE_ACL,
            _PROVIDER_WRITABLE_FILE_ACL,
        }:
            raise DeploymentOperationRejected(
                "Interpreter endpoint configuration must remain operator-owned"
            )
    _grant_provider_tree_access(root)


def _read_owned_private_credential(
    path: Path,
    operation: str,
    *,
    require_provider_access: bool = False,
) -> tuple[bytes, ProviderSigningCredential]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise DeploymentOperationRejected(
            f"{operation} is unavailable"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_size > PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES
        ):
            raise DeploymentOperationRejected(
                f"{operation} must be operator-owned with exact private access"
            )
        mode = stat.S_IMODE(metadata.st_mode)
        access = _read_acl(path, operation)
        private_access = mode == 0o600 and access == _PRIVATE_WRITABLE_FILE_ACL
        provider_access = (
            mode == 0o640 and access == _PROVIDER_WRITABLE_FILE_ACL
        )
        if (
            (require_provider_access and not provider_access)
            or (
                not require_provider_access
                and not (private_access or provider_access)
            )
        ):
            raise DeploymentOperationRejected(
                f"{operation} must be operator-owned with exact private access"
            )
        raw = os.read(descriptor, PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES + 1)
        if len(raw) != metadata.st_size:
            raise DeploymentOperationRejected(
                f"{operation} changed while it was read"
            )
    finally:
        os.close(descriptor)
    try:
        credential = parse_provider_signing_credential(raw)
    except ValueError as error:
        raise DeploymentOperationRejected(
            f"{operation} is invalid"
        ) from error
    return raw, credential


def _select_api_credential(
    nmr_api_v1: Path,
    provider_ref: str,
) -> tuple[bytes, ProviderSigningCredential]:
    try:
        api_root = nmr_api_v1.resolve(strict=True)
    except OSError as error:
        raise DeploymentOperationRejected(
            "NMR API checkout is unavailable"
        ) from error
    if api_root != nmr_api_v1 or not api_root.is_dir():
        raise DeploymentOperationRejected(
            "NMR API checkout must be one resolved non-symlink directory"
        )
    provider_id = provider_ref.removeprefix("provider:")
    credential_root = api_root / "secrets"
    candidates = sorted(
        credential_root.glob(f"*/providers/{provider_id}/signing.private.json")
    )
    admitted: list[tuple[bytes, ProviderSigningCredential]] = []
    for candidate in candidates:
        relative = candidate.relative_to(credential_root)
        if len(relative.parts) != 4 or candidate.resolve(strict=True) != candidate:
            raise DeploymentOperationRejected(
                "API-issued provider signing credential path is invalid"
            )
        raw, credential = _read_owned_private_credential(
            candidate,
            "API-issued provider signing credential",
        )
        if (
            credential.profile != relative.parts[0]
            or credential.provider_ref != provider_ref
        ):
            raise DeploymentOperationRejected(
                "API-issued provider signing credential identity does not match its path"
            )
        admitted.append((raw, credential))
    if len(admitted) != 1:
        raise DeploymentOperationRejected(
            "NMR API checkout must contain exactly one matching provider credential"
        )
    return admitted[0]


def _require_stopped_project(
    docker: Path,
    repository: Path,
    deployment: str,
) -> None:
    services = _inspect_project_containers(docker, repository, deployment)
    _require_stopped_services(services, "Provider credential replacement")


def _require_stopped_services(
    services: dict[str, dict[str, object]],
    operation: str,
) -> None:
    for record in services.values():
        state = record.get("State")
        if type(state) is not dict or state.get("Running") is not False:
            raise DeploymentOperationRejected(
                f"{operation} requires a proved-stopped deployment"
            )


def _runtime_config(
    repository: Path,
    deployment: str,
) -> ProviderRuntimeConfig:
    try:
        configured = decode_provider_runtime_config(
            _read_regular_file(
                repository / "config/deployments" / deployment / "provider.toml"
            )
        )
    except (OSError, TypeError, ValueError) as error:
        raise DeploymentOperationRejected(
            "Deployment runtime config is invalid"
        ) from error
    return configured


def _journal_runtime_config(
    repository: Path,
    deployment: str,
) -> ProviderRuntimeConfig:
    configured = _runtime_config(repository, deployment)
    if configured.endpoint.ca_file is not None:
        raise DeploymentOperationRejected(
            "Provider journal retirement does not support a container-only private CA"
        )
    return configured


@contextmanager
def _held_provider_identity_lock(
    docker: Path,
    lock_volume: str,
    provider_ref: str,
    provider_image: LocalImage,
):
    with TemporaryDirectory() as temporary:
        container_id_path = Path(temporary) / "holder.cid"
        with _run_held_provider_identity_lock(
            docker,
            lock_volume,
            provider_ref,
            provider_image,
            container_id_path,
        ):
            yield


@contextmanager
def _run_held_provider_identity_lock(
    docker: Path,
    lock_volume: str,
    provider_ref: str,
    provider_image: LocalImage,
    container_id_path: Path,
):
    arguments = (
        str(docker),
        "--context",
        "default",
        "run",
        "--rm",
        "--interactive",
        "--cidfile",
        str(container_id_path),
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--user",
        "65532:65532",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "32",
        "--memory",
        "128m",
        "--memory-swap",
        "128m",
        "--log-driver",
        "none",
        "--mount",
        f"type=volume,src={lock_volume},dst=/run/nmrpeak-provider-lock,readonly",
        "--entrypoint",
        "python",
        provider_image.image_id,
        "-m",
        "nmrpeak_provider.identity_lock_hold",
        "/run/nmrpeak-provider-lock/provider.lock",
        provider_ref,
    )
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                "DOCKER_CONTEXT": "default",
            },
        )
    except OSError as error:
        raise DeploymentOperationRejected(
            "Provider identity-lock holder could not start"
        ) from error
    assert process.stdin is not None
    assert process.stdout is not None
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        readable, _, _ = select.select((process.stdout,), (), (), 30)
        ready = process.stdout.readline(64) if readable else b""
        if ready != b"READY\n" or process.poll() is not None:
            raise DeploymentOperationRejected(
                "Provider identity-lock holder did not acquire the engine-global lock"
            )
        yield
        if process.poll() is not None:
            raise DeploymentOperationRejected(
                "Provider identity-lock holder exited during journal retirement"
            )
    except BaseException as error:
        primary_error = error
    finally:
        try:
            _stop_identity_lock_holder(process, docker, container_id_path)
        except BaseException as error:
            cleanup_error = error
    if cleanup_error is not None:
        if primary_error is not None:
            raise DeploymentOperationRejected(
                "Provider identity-lock holder cleanup failed after the operation failed"
            ) from primary_error
        raise cleanup_error
    if primary_error is not None:
        raise primary_error


def _stop_identity_lock_holder(
    process: subprocess.Popen[bytes],
    docker: Path,
    container_id_path: Path,
) -> None:
    assert process.stdin is not None
    process.stdin.close()
    try:
        returncode = process.wait(timeout=30)
    except subprocess.TimeoutExpired as timeout_error:
        container_error: BaseException | None = None
        client_error: BaseException | None = None
        try:
            container_id = _read_holder_container_id(container_id_path)
            _remove_identity_lock_holder_container(docker, container_id)
        except BaseException as error:
            container_error = error
        try:
            _reap_docker_client(process)
        except BaseException as error:
            client_error = error
        if container_error is not None and client_error is not None:
            raise DeploymentOperationRejected(
                "Provider identity-lock holder container cleanup and client reap failed"
            ) from container_error
        if container_error is not None:
            raise container_error
        if client_error is not None:
            raise client_error
        raise DeploymentOperationRejected(
            "Provider identity-lock holder required forced container removal"
        ) from timeout_error
    container_id = _read_holder_container_id(container_id_path)
    if _holder_container_exists(docker, container_id):
        _remove_identity_lock_holder_container(docker, container_id)
        raise DeploymentOperationRejected(
            "Provider identity-lock holder remained after its Docker client exited"
        )
    if returncode != 0:
        raise DeploymentOperationRejected(
            "Provider identity-lock holder did not stop cleanly"
        )


def _read_holder_container_id(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise DeploymentOperationRejected(
            "Provider identity-lock holder container identity is unavailable"
        ) from error
    try:
        status = os.fstat(descriptor)
        raw = os.read(descriptor, 65)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or re.fullmatch(rb"[0-9a-f]{64}\n?", raw) is None
    ):
        raise DeploymentOperationRejected(
            "Provider identity-lock holder container identity is invalid"
        )
    return raw.decode("ascii").strip()


def _holder_container_exists(docker: Path, container_id: str) -> bool:
    lines = _docker_command(
        docker,
        (
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--quiet",
            "--filter",
            f"id={container_id}",
        ),
        timeout=30,
    ).stdout.decode("ascii", errors="strict").splitlines()
    if lines not in ([], [container_id]):
        raise DeploymentOperationRejected(
            "Docker returned an ambiguous identity-lock holder inventory"
        )
    return bool(lines)


def _remove_identity_lock_holder_container(
    docker: Path,
    container_id: str,
) -> None:
    stop_error: BaseException | None = None
    if _holder_container_exists(docker, container_id):
        try:
            stopped = _docker_command(
                docker,
                ("container", "stop", "--time", "5", container_id),
                timeout=15,
            ).stdout.decode("ascii", errors="strict").strip()
            if stopped != container_id:
                raise DeploymentOperationRejected(
                    "Docker did not confirm the identity-lock holder stop"
                )
        except BaseException as error:
            stop_error = error
    if _holder_container_exists(docker, container_id):
        removed = _docker_command(
            docker,
            ("container", "rm", "--force", container_id),
            timeout=15,
        ).stdout.decode("ascii", errors="strict").strip()
        if removed != container_id:
            raise DeploymentOperationRejected(
                "Docker did not confirm the identity-lock holder removal"
            )
    if _holder_container_exists(docker, container_id):
        raise DeploymentOperationRejected(
            "Provider identity-lock holder container remains after cleanup"
        )
    if stop_error is not None:
        raise DeploymentOperationRejected(
            "Provider identity-lock holder required forced removal after stop failed"
        ) from stop_error


def _reap_docker_client(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as error:
        raise DeploymentOperationRejected(
            "Provider identity-lock holder Docker client could not be reaped"
        ) from error


def _journal_generation_ids(
    docker: Path,
    repository: Path,
    deployment: str,
    provider_ref: str,
    authority_id: str,
    services: dict[str, dict[str, object]],
) -> tuple[str, ...]:
    journal_volume, attachments = inspect_provider_journal_volume(
        docker,
        deployment,
        provider_ref,
        authority_id,
    )
    project_containers = {record["Id"] for record in services.values()}
    if not set(attachments).issubset(project_containers):
        raise DeploymentOperationRejected(
            "Provider journal volume has a foreign container attachment"
        )
    provider_image = _resolve_provider_image(docker, repository)
    result = _docker_command(
        docker,
        (
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "32",
            "--memory",
            "128m",
            "--memory-swap",
            "128m",
            "--log-driver",
            "none",
            "--mount",
            f"type=volume,src={journal_volume},dst=/var/lib/nmrpeak-provider,readonly",
            "--entrypoint",
            "python",
            provider_image.image_id,
            "-m",
            "nmrpeak_provider.journal_inventory",
        ),
        timeout=300,
    )
    if not result.stdout.endswith(b"\n") or result.stdout.endswith(b"\n\n"):
        raise DeploymentOperationRejected(
            "Provider journal inventory returned invalid framing"
        )
    try:
        document = parse_canonical_json_bytes(result.stdout[:-1])
    except (TypeError, ValueError) as error:
        raise DeploymentOperationRejected(
            "Provider journal inventory returned invalid canonical JSON"
        ) from error
    if type(document) is not dict or set(document) != {
        "schema_id",
        "frozen_generation_ids",
    }:
        raise DeploymentOperationRejected(
            "Provider journal inventory returned an invalid shape"
        )
    values = document["frozen_generation_ids"]
    if (
        document["schema_id"] != "nmrpeak.journal_generation_inventory.v1"
        or type(values) is not list
        or any(type(value) is not str for value in values)
        or values != sorted(set(values))
    ):
        raise DeploymentOperationRejected(
            "Provider journal inventory returned invalid generation identities"
        )
    try:
        for value in values:
            validate_frozen_generation_id(value)
    except (TypeError, ValueError) as error:
        raise DeploymentOperationRejected(
            "Provider journal inventory returned invalid generation identities"
        ) from error
    return tuple(values)


def _remove_retained_generation(
    state_root: Path,
    frozen_generation: str,
) -> None:
    generations = state_root / "generations"
    try:
        generations_status = generations.stat(follow_symlinks=False)
    except OSError as error:
        raise DeploymentOperationRejected(
            "Retained generation directory is unavailable"
        ) from error
    if (
        not stat.S_ISDIR(generations_status.st_mode)
        or generations_status.st_uid != os.geteuid()
        or stat.S_IMODE(generations_status.st_mode) != 0o700
    ):
        raise DeploymentOperationRejected(
            "Retained generation directory must be operator-owned mode 0700"
        )
    retained_name = _retained_identity_name(frozen_generation)
    target = generations / retained_name
    frozen_root = target / "frozen"
    manifest = _read_regular_file(frozen_root / "manifest.json")
    try:
        loaded = load_frozen_generation(
            frozen_root,
            expected_frozen_generation_id=frozen_generation,
        )
    except (OSError, TypeError, ValueError) as error:
        raise DeploymentOperationRejected(
            "Retained frozen generation is invalid"
        ) from error
    expected = {"frozen/manifest.json": manifest}
    expected.update(
        {
            f"frozen/{frozen_file.path}": frozen_file.content
            for frozen_file in loaded.files
        }
    )
    _require_retained_tree(target, expected)
    staged = generations / f".{retained_name}.{secrets.token_hex(16)}.removing"
    renamed = False
    deleted = False
    try:
        os.rename(target, staged)
        renamed = True
        _fsync_directory(generations)
        shutil.rmtree(staged)
        deleted = True
        _fsync_directory(generations)
    except OSError as error:
        if deleted:
            raise DeploymentOperationRejected(
                "Frozen generation was removed, but deletion durability is unconfirmed"
            ) from error
        if renamed:
            raise DeploymentOperationRejected(
                "Frozen generation removal was incomplete; staged residue remains"
            ) from error
        raise DeploymentOperationRejected(
            "Frozen generation could not be staged for removal"
        ) from error


def _publish_private_credential(
    destination: Path,
    content: bytes,
    *,
    replace: bool,
) -> None:
    parent_fd = os.open(
        destination.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    stage = f".{destination.name}.{secrets.token_hex(16)}.staging"
    published = False
    try:
        _write_new_file(parent_fd, stage, content)
        _grant_provider_file_access(destination.parent / stage, owner_write=True)
        if replace:
            os.rename(
                stage,
                destination.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            published = True
        else:
            try:
                os.link(
                    stage,
                    destination.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise DeploymentOperationRejected(
                    "Provider signing credential appeared during installation"
                ) from error
            published = True
            os.unlink(stage, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except (DeploymentOperationRejected, OSError) as error:
        try:
            os.unlink(stage, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        if published:
            raise DeploymentOperationRejected(
                "Provider credential publication did not complete durably; "
                "installed state may have changed"
            ) from error
        raise
    finally:
        os.close(parent_fd)


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
    retained_name = _retained_identity_name(identity)
    destination = parent / retained_name
    if destination.exists() or destination.is_symlink():
        _require_retained_tree(destination, files)
        _grant_provider_tree_access(destination)
        _require_retained_tree(destination, files, require_provider_access=True)
        return destination
    stage = parent / f".{retained_name}.{secrets.token_hex(16)}.staging"
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
        _grant_provider_tree_access(stage)
        _fsync_tree(stage)
        os.rename(stage, destination)
        _fsync_directory(parent)
    except BaseException:
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage)
        raise
    _require_retained_tree(destination, files, require_provider_access=True)
    return destination


def _retained_identity_name(identity: str) -> str:
    return identity.removeprefix("sha256:")


def _require_retained_tree(
    path: Path,
    expected: dict[str, bytes],
    *,
    require_provider_access: bool = False,
) -> None:
    if path.is_symlink() or not path.is_dir():
        raise DeploymentOperationRejected(
            f"Retained deployment input is not a directory: {path.name}"
        )
    actual_files: dict[str, bytes] = {}
    actual_directories: set[str] = set()
    for directory, names, filenames in os.walk(path, followlinks=False):
        current = Path(directory)
        metadata = current.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise DeploymentOperationRejected(
                f"Retained deployment directory posture has drifted: {path.name}"
            )
        mode = stat.S_IMODE(metadata.st_mode)
        access = _read_acl(current, "Retained deployment directory")
        private_access = mode == 0o700 and access == _PRIVATE_DIRECTORY_ACL
        provider_access = mode == 0o750 and access == _PROVIDER_DIRECTORY_ACL
        if (
            (require_provider_access and not provider_access)
            or (
                not require_provider_access
                and not (private_access or provider_access)
            )
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
                ):
                    raise DeploymentOperationRejected(
                        "Retained deployment file posture has drifted: "
                        f"{path.name}"
                    )
                mode = stat.S_IMODE(metadata.st_mode)
                access = _read_acl(child, "Retained deployment file")
                private_access = (
                    mode == 0o400 and access == _PRIVATE_READONLY_FILE_ACL
                )
                provider_access = (
                    mode == 0o440 and access == _PROVIDER_READONLY_FILE_ACL
                )
                if (
                    (require_provider_access and not provider_access)
                    or (
                        not require_provider_access
                        and not (private_access or provider_access)
                    )
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


def _grant_provider_tree_access(root: Path) -> None:
    directories: list[Path] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        directories.append(current)
        for name in (*names, *filenames):
            if (current / name).is_symlink():
                raise DeploymentOperationRejected(
                    "Retained deployment input contains a symlink"
                )
        for name in filenames:
            _grant_provider_file_access(current / name, owner_write=False)
    for directory in reversed(directories):
        _replace_acl(directory, _PROVIDER_DIRECTORY_ACL, directory=True)


def _grant_provider_file_access(path: Path, *, owner_write: bool) -> None:
    access = (
        _PROVIDER_WRITABLE_FILE_ACL
        if owner_write
        else _PROVIDER_READONLY_FILE_ACL
    )
    _replace_acl(path, access, directory=False)


def _replace_acl(
    path: Path,
    access: tuple[str, ...],
    *,
    directory: bool,
) -> None:
    if _read_acl(path, "Provider input") == access:
        return
    if directory:
        _run_acl_command(_SETFACL, "--physical", "--remove-default", "--", str(path))
    _run_acl_command(
        _SETFACL,
        "--physical",
        f"--set={','.join(access)}",
        "--",
        str(path),
    )
    if _read_acl(path, "Provider input") != access:
        raise DeploymentOperationRejected(
            f"Provider input access could not be proved: {path.name}"
        )


def _read_acl(path: Path, operation: str) -> tuple[str, ...]:
    raw = _run_acl_command(
        _GETFACL,
        "--physical",
        "--absolute-names",
        "--omit-header",
        "--numeric",
        "--",
        str(path),
    )
    try:
        return tuple(line for line in raw.decode("ascii").splitlines() if line)
    except UnicodeDecodeError as error:
        raise DeploymentOperationRejected(
            f"{operation} access record is invalid: {path.name}"
        ) from error


def _run_acl_command(executable: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            (str(executable), *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentOperationRejected(
            "Provider input access operation did not complete"
        ) from error
    if result.returncode != 0 or len(result.stdout) > 65_536 or result.stderr:
        raise DeploymentOperationRejected(
            "Provider input access operation was rejected"
        )
    return result.stdout


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


def _admit_localhost_ca_certificate(path: Path | None) -> Path | None:
    if path is None:
        return None
    if not isinstance(path, Path) or not path.is_absolute():
        raise DeploymentOperationRejected(
            "Localhost CA certificate must be an absolute path"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DeploymentOperationRejected(
            "Localhost CA certificate is unavailable"
        ) from error
    if resolved != path:
        raise DeploymentOperationRejected(
            "Localhost CA certificate must be a resolved non-symlink path"
        )
    if not _read_regular_file(path):
        raise DeploymentOperationRejected(
            "Localhost CA certificate must not be empty"
        )
    return path


def _admit_server_a_endpoint(
    endpoint: ProviderEndpointConfig,
    *,
    localhost: bool,
) -> None:
    if localhost:
        if (
            endpoint.origin != "https://nmr.localhost:10443"
            or endpoint.expected_topology != "dev-local"
            or endpoint.ca_file is None
        ):
            raise DeploymentOperationRejected(
                "Localhost deployment requires the dev-local nmr.localhost origin "
                "and private Server A CA"
            )
    elif endpoint.ca_file is not None:
        raise DeploymentOperationRejected(
            "Public deployment config cannot select a private Server A CA"
        )


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
        "provider": _provider_image_spec(input_ids["provider"]),
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


def _resolve_provider_image(docker: Path, repository: Path) -> LocalImage:
    revision = _committed_revision(repository)
    with TemporaryDirectory() as temporary:
        context = Path(temporary) / "provider"
        context.mkdir()
        input_id = materialize_image_context(
            repository,
            revision,
            context,
            "provider",
        )
    return resolve_local_image(docker, _provider_image_spec(input_id))


def _provider_image_spec(input_id: str) -> LocalImageSpec:
    return LocalImageSpec(
        "numpde/nmrpeak-provider",
        input_id,
        (("io.numpde.nmrpeak.provider.contract-id", "nmr.provider.http.v1"),),
        _PROVIDER_ENTRYPOINT,
    )


def _compose_environment(
    repository: Path,
    deployment: str,
    provider_ref: str,
    images: DeploymentImages,
    checkpoints: DeploymentCheckpoints,
    *,
    provider_config: Path,
    frozen_generation: Path,
    localhost_ca_certificate: Path | None,
) -> dict[str, str]:
    state_root = repository / "secrets/deployments" / deployment
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "DOCKER_CONTEXT": "default",
        "IMAGE_PLATFORM": "linux/amd64",
        "PROVIDER_IMAGE_REF": images.provider,
        "HF_RUNNER_IMAGE_REF": images.hf,
        "CHF_RUNNER_IMAGE_REF": images.chf,
        "PROVIDER_CONFIG_PATH": str(provider_config),
        "PROVIDER_CREDENTIAL_PATH": str(state_root / "signing.private.json"),
        "INTERPRETER_CONFIG_PATH": str(
            state_root / "openai-chat-completions.d"
        ),
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
    if localhost_ca_certificate is not None:
        environment["LOCALHOST_CA_CERTIFICATE_PATH"] = str(
            localhost_ca_certificate
        )
    return environment


def _render_compose(
    repository: Path,
    deployment: str,
    environment: dict[str, str],
    docker: Path,
    *,
    localhost: bool,
) -> dict[str, object]:
    project = f"nmrpeak-{deployment}"
    compose_files = [
        "--file",
        str(repository / "compose/provider.yml"),
    ]
    if localhost:
        compose_files.extend(
            (
                "--file",
                str(repository / "compose/provider-localhost.yml"),
            )
        )
    try:
        result = subprocess.run(
            (
                str(docker),
                "compose",
                "--env-file",
                "/dev/null",
                "--project-name",
                project,
                *compose_files,
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
    parser.add_argument(
        "operation",
        choices=(
            "config",
            "credential-install",
            "down",
            "generation-remove",
            "init",
            "journal-retire",
            "logs",
            "status",
            "up",
        ),
    )
    parser.add_argument("deployment")
    parser.add_argument("--nmr-api-v1", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--frozen-generation")
    parser.add_argument("--confirm")
    parser.add_argument("--localhost-ca-certificate", type=Path)
    options = parser.parse_args(arguments)
    repository = Path(__file__).resolve().parents[1]
    try:
        if options.operation == "credential-install":
            if (
                options.nmr_api_v1 is None
                or options.frozen_generation is not None
                or options.confirm is not None
                or options.localhost_ca_certificate is not None
            ):
                parser.error("credential-install requires --nmr-api-v1")
            installed = install_provider_credential(
                repository,
                options.deployment,
                options.nmr_api_v1,
                replace=options.replace,
            )
            print(f"Installed provider credential: {installed}")
        elif options.operation == "generation-remove":
            if (
                options.frozen_generation is None
                or options.confirm is None
                or options.nmr_api_v1 is not None
                or options.replace
                or options.localhost_ca_certificate is not None
            ):
                parser.error(
                    "generation-remove requires --frozen-generation and --confirm"
                )
            remove_frozen_generation(
                repository,
                options.deployment,
                options.frozen_generation,
                options.confirm,
            )
            print(
                "Removed frozen deployment generation: "
                f"{options.deployment} {options.frozen_generation}"
            )
        elif options.operation == "journal-retire":
            if (
                options.confirm is None
                or options.nmr_api_v1 is not None
                or options.replace
                or options.frozen_generation is not None
                or options.localhost_ca_certificate is not None
            ):
                parser.error("journal-retire requires --confirm")
            removed = retire_provider_journal(
                repository,
                options.deployment,
                options.confirm,
            )
            print(
                f"Retired provider journal volume: {removed}. "
                "Docker volume deletion is not secure erasure of underlying storage."
            )
        elif options.operation not in {"config", "up"} and (
            options.nmr_api_v1 is not None
            or options.replace
            or options.frozen_generation is not None
            or options.confirm is not None
            or options.localhost_ca_certificate is not None
        ):
            parser.error("operation-specific options do not match the operation")
        elif options.operation == "init":
            initialized = initialize_deployment(repository, options.deployment)
            print(f"Initialized deployment {options.deployment}: {initialized}")
        elif options.operation == "config":
            plan = render_deployment_plan(
                repository,
                options.deployment,
                localhost_ca_certificate=options.localhost_ca_certificate,
            )
            print(deployment_plan_bytes(plan).decode("utf-8"))
        elif options.operation == "up":
            plan = start_deployment(
                repository,
                options.deployment,
                localhost_ca_certificate=options.localhost_ca_certificate,
            )
            print(
                "Provider deployment ready: "
                f"{options.deployment} {plan.generation.frozen_generation_id}"
            )
        elif options.operation == "status":
            print(deployment_status_bytes(repository, options.deployment).decode("utf-8"))
        elif options.operation == "logs":
            show_provider_logs(repository, options.deployment)
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
