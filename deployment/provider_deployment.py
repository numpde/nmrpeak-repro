"""Initialize one literal named deployment from committed examples."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
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
from nmrpeak_provider.diagnostic_text import diagnostic_text, exception_diagnostic
from nmrpeak_provider.local_input import LocalInputSnapshotError, read_bounded_regular_file
from nmrpeak_provider.provider_config import (
    CONFIG_PATH, api_input_target, decode_provider_template,
    render_provider_runtime_config, runtime_config_id,
)
from nmrpeak_provider.inspection_document import validate_inspection_document
from nmrpeak_provider.archive_document import validate_archive_document
from nmrpeak_provider.canonical_json import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from nmrpeak_provider.attempt_journal import validate_frozen_generation_id
from nmrpeak_provider.frozen_generation import (
    frozen_generation_id,
    load_frozen_generation,
)
from nmrpeak_provider.interpreter_policy import (
    MAX_INTERPRETER_CONFIG_BYTES,
    MAX_INTERPRETER_ENDPOINTS,
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
_MAX_FILE_BYTES = 1_048_576
_SESSION_VOLUME_OPTIONS = {
    "device": "tmpfs", "o": "size=1m,uid=65532,gid=65532,mode=0700", "type": "tmpfs",
}
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
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
    '/usr/local/bin/python3.12',
    '-P',
    '-m',
    'nmrpeak_provider.native_launcher',
    'nmrpeak_hf_v1',
    'models.nmrpeak_hf_v1.runner.worker',
)
_CHF_ENTRYPOINT = (
    '/usr/local/bin/python3.12',
    '-P',
    '-m',
    'nmrpeak_provider.native_launcher',
    'nmrpeak_chf_v1',
    'models.nmrpeak_chf_v1.runner.worker',
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
    runtime_files: dict[str, bytes] = field(repr=False)
    binding_projection: dict = field(repr=False)


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


def _read_capture_source(path: Path, *, secret: bool) -> bytes:
    """Keep source ownership/privacy intact while taking a bounded snapshot."""
    try:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}:
            raise DeploymentOperationRejected(f"Cannot capture startup input {path}: expected a regular file owned by root or operator UID {os.geteuid()}; found UID {metadata.st_uid} and mode {stat.S_IMODE(metadata.st_mode):04o}")
        if secret:
            access = _read_acl(path, "Captured private startup input")
            if metadata.st_uid != os.geteuid() or (stat.S_IMODE(metadata.st_mode), access) not in {
                (0o600, _PRIVATE_WRITABLE_FILE_ACL), (0o400, _PRIVATE_READONLY_FILE_ACL),
                (0o440, _PROVIDER_READONLY_FILE_ACL), (0o640, _PROVIDER_WRITABLE_FILE_ACL),
            }:
                raise DeploymentOperationRejected(f"Cannot capture private startup input {path}: UID {metadata.st_uid}, mode {stat.S_IMODE(metadata.st_mode):04o} and its ACL must match operator-private or provider-read-only access")
        elif metadata.st_mode & 0o022:
            raise DeploymentOperationRejected(f"Cannot capture public startup input {path}: mode {stat.S_IMODE(metadata.st_mode):04o} permits group or other users to change it")
        return read_bounded_regular_file(path, maximum_bytes=1048576)
    except (OSError, LocalInputSnapshotError) as cause:
        raise DeploymentOperationRejected(f"Cannot capture startup input {path}: its bounded regular-file read failed; preserve the source and inspect its permissions") from cause


def _contained_binding_projection(docker, image, definition, binding_name, namespace, *, input_sources):
    """Run SDK binding/route admission as the source operator, without networking."""
    with TemporaryDirectory() as temporary:
        source = Path(temporary) / "bindings.toml"
        source.write_bytes(definition)
        source.chmod(0o600)
        mappings = tuple(value for path, captured in sorted(input_sources.items())
                         for value in ("--input-source", path, captured))
        result = _docker_command(docker, (
            "run", "--pull", "never", "--rm", "--network", "none", "--read-only",
            "--user", f"{os.geteuid()}:{os.getegid()}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--pids-limit", "64",
            "--cpus", "1", "--memory", "256m", "--env", "PROVIDER_CORE_IN_CONTAINER=1",
            "--mount", f"type=bind,source={source},target=/binding/definition.toml,readonly",
            "--entrypoint", "/usr/local/bin/python3.12", image,
            "-I", "-B", "-m", "nmrpeak_provider.deployment_runtime", "project",
            "--definition", "/binding/definition.toml", "--binding", binding_name,
            "--image", image, "--namespace", namespace, *mappings,
        ), timeout=60)
    document = _json_output(result.stdout, "Contained API binding projection")
    if type(document) is not dict:
        raise DeploymentOperationRejected("Contained API binding projection must be an object")
    return document


def _capture_interpreter_inputs(state_root):
    """Snapshot the existing bounded endpoint inventory without parsing its format."""
    root = state_root / "openai-chat-completions.d"
    try:
        metadata = root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise DeploymentOperationRejected("Interpreter endpoint directory must be a regular operator-owned directory")
        files = {}
        for entry in root.iterdir():
            if len(files) == MAX_INTERPRETER_ENDPOINTS or entry.suffix != ".toml":
                raise DeploymentOperationRejected("Interpreter endpoint inventory exceeds its limit or contains a non-TOML entry")
            raw = _read_capture_source(entry, secret=True)
            if len(raw) > MAX_INTERPRETER_CONFIG_BYTES:
                raise DeploymentOperationRejected("Interpreter endpoint input exceeds its configured byte limit")
            files["interpreter/" + entry.name] = raw
        if not files:
            raise DeploymentOperationRejected("Interpreter endpoint inventory must contain at least one TOML input")
        return files
    except OSError as cause:
        raise DeploymentOperationRejected("Cannot capture the interpreter endpoint directory") from cause


def _validate_captured_runtime(docker, image, files, *, arguments=("validate",)):
    """Validate the exact private snapshot as the runtime UID before installation."""
    with TemporaryDirectory() as temporary:
        parent = Path(temporary)
        captured = _publish_retained_tree(parent, runtime_config_id(files), files)
        result = _docker_command(docker, (
            "run", "--pull", "never", "--rm", "--network", "none", "--read-only",
            "--user", "65532:65532", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "64", "--cpus", "1", "--memory", "256m",
            "--env", "PROVIDER_CORE_IN_CONTAINER=1",
            "--mount", f"type=bind,source={captured},target={CONFIG_PATH.parent},readonly",
            "--entrypoint", "/usr/local/bin/python3.12", image,
            "-I", "-B", "-m", "nmrpeak_provider.deployment_runtime", *arguments,
        ), timeout=60)
    document = _json_output(result.stdout, "Contained captured runtime validation")
    if type(document) is not dict:
        raise DeploymentOperationRejected("Contained captured runtime validation must be an object")
    return document


def _journal_binding(repository, deployment, docker, image, provider_ref, *, namespace):
    policy = decode_provider_template(_read_regular_file(
        repository / "config/deployments" / deployment / "provider.toml"))
    definition = _read_capture_source(policy.binding_definition, secret=False)
    projection = _contained_binding_projection(docker, image.image_id, definition,
                                               policy.binding_name, namespace, input_sources={})
    if projection["provider_ref"] != provider_ref:
        raise DeploymentOperationRejected("Selected API binding belongs to another journal provider")
    return policy, definition, projection


def _cleanup_maintenance_route(docker, repository, deployment, projection):
    """Consume this invocation's original SDK grant through the existing teardown owner."""
    route = projection["api_route"]["compose"]
    volumes = tuple(item["name"] for item in route["volumes"].values())
    services = _inspect_project_containers(docker, repository, deployment, roles=frozenset(route["services"]))
    for role, record in services.items():
        expected = route["services"][role]
        labels = record["Config"]["Labels"]
        if record.get("Image") != expected["image"] or any(labels.get(key) != value for key, value in expected["labels"].items()):
            raise DeploymentOperationRejected("Maintenance API route no longer matches its original SDK image and labels")
    observed = _api_route_volumes(deployment, services)
    if not set(observed).issubset(volumes):
        raise DeploymentOperationRejected("Maintenance API route mounts differ from this invocation's original grant")
    _inspect_project_resources(docker, deployment, services, route_volumes=volumes, scientific_sessions=False)
    _run_compose_down(docker, repository, deployment, route_volumes=volumes, api_route=route)
    remaining = _inspect_project_containers(docker, repository, deployment, roles=frozenset(route["services"]))
    resources = _inspect_project_resources(docker, deployment, remaining, route_volumes=volumes, scientific_sessions=False)
    if remaining or resources:
        raise DeploymentOperationRejected("Maintenance API route teardown left owned resources; preserve the journal")


@contextmanager
def _held_maintenance_api_route(docker, repository, deployment, image, provider_ref, authority_id):
    """Capture API inputs and own the exact temporary route through bounded teardown."""
    invocation = "maintenance-" + secrets.token_hex(16)
    namespace = "nmrpeak-" + invocation
    policy, definition, projection = _journal_binding(repository, deployment, docker, image,
                                                      provider_ref, namespace=namespace)
    if projection["journal_authority_id"] != authority_id:
        raise DeploymentOperationRejected("Selected API namespace changed before maintenance capture; preserve the journal")
    files = {"bindings.toml": definition}
    for source in projection["input_paths"]:
        leaf = api_input_target(source).relative_to(CONFIG_PATH.parent).as_posix()
        files[leaf] = _read_capture_source(Path(source), secret=source == projection["credential_file"])
    with TemporaryDirectory() as temporary:
        captured = _publish_retained_tree(Path(temporary), runtime_config_id(files), files)
        sources = {source: str(captured / api_input_target(source).relative_to(CONFIG_PATH.parent))
                   for source in projection["input_paths"]}
        projection = _contained_binding_projection(docker, image.image_id, definition,
                       policy.binding_name, namespace, input_sources=sources)
        socket = projection["api_route"]["client_socket"]
        validated = _validate_captured_runtime(docker, image.image_id, files,
                    arguments=("validate-binding", "--binding", policy.binding_name, "--proxy-socket", socket))
        expected = {key: projection[key] for key in ("provider_ref", "api_origin", "api_topology", "journal_authority_id")}
        if validated != expected | {"proxy_socket": socket}:
            raise DeploymentOperationRejected("Maintenance API capture differs from its selected SDK binding")
        compose = Path(temporary) / "api-route.json"
        compose.write_bytes(canonical_json_bytes(projection["api_route"]["compose"]))
        prefix = ("compose", "--env-file", "/dev/null", "--project-directory", str(repository),
                  "--project-name", namespace, "--file", str(compose))
        primary = None
        try:
            _docker_command(docker, (*prefix, "up", "--detach", "--wait", "--wait-timeout", "60",
                                     "--no-build", "--pull", "never"), timeout=80)
            yield policy, projection, captured
        except BaseException as cause:
            primary = cause
            raise
        finally:
            try:
                _cleanup_maintenance_route(docker, repository, invocation, projection)
            except BaseException as cleanup:
                cause = cleanup if primary is None or cleanup is primary else BaseExceptionGroup(
                    "Maintenance operation and API route teardown failed", [primary, cleanup])
                raise DeploymentOperationRejected(
                    f"Cannot confirm maintenance route {namespace} was removed; preserve the journal and inspect that exact project before retrying") from cause


def _compose_binding_route(document, projection):
    """Merge the exact SDK projection and grant its client/delivery endpoints."""
    route = projection["api_route"]
    for field in ("services", "networks", "volumes"):
        fragment = route["compose"][field]
        if document[field].keys() & fragment.keys():
            raise DeploymentOperationRejected("SDK API route collides with an existing deployment role or resource")
        document[field].update(fragment)
    delivery = projection["delivery"]
    document["volumes"]["delivery-ipc"] = {"external": True, "name": delivery["ipc_volume"]}
    provider = document["services"]["provider"]
    provider.setdefault("volumes", []).extend((route["client_mount"], {
        "type": "volume", "source": "delivery-ipc", "target": str(Path(delivery["socket_path"]).parent),
        "read_only": True,
    }))
    provider["depends_on"] = route["client_depends_on"]
    provider["group_add"] = [str(delivery["socket_group_id"])]
    return document


def render_deployment_plan(
    repository: Path,
    deployment: str,
    *,
    docker: Path = _DOCKER,
) -> DeploymentPlan:
    """Freeze selected inputs and admit their actual contained runtime composition."""
    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected("Deployment repository must be one resolved directory")
    _require_deployment_name(deployment)
    _require_clean_checkout(root)
    config_root = root / "config/deployments" / deployment
    try:
        if config_root.resolve(strict=True) != config_root or not config_root.is_dir():
            raise DeploymentOperationRejected("Deployment config root must be a non-symlink directory")
    except OSError as cause:
        raise DeploymentOperationRejected(f"Deployment is not initialized: {deployment}") from cause
    selection = load_named_deployment(config_root / "deployment.toml")
    policy = decode_provider_template(_read_regular_file(config_root / "provider.toml"))
    definition = _read_capture_source(policy.binding_definition, secret=False)
    upstream_revision = read_nmrpeak_source_revision(root / "families/nmrpeak/source-closure.paths")
    releases = admit_deployment_releases(
        selection,
        hf_release_declaration=_release_declaration(root / "models/nmrpeak_hf_v1/releases", selection.hf.release_name),
        chf_release_declaration=_release_declaration(root / "models/nmrpeak_chf_v1/releases", selection.chf.release_name),
        upstream_revision=upstream_revision,
    )
    revision = _committed_revision(root)
    input_ids = _image_input_ids(root, revision)
    local_images = _local_images(docker, input_ids)
    images = DeploymentImages(local_images["provider"].image_id, input_ids["provider"],
                              local_images["hf"].image_id, input_ids["hf"],
                              local_images["chf"].image_id, input_ids["chf"])
    checkpoints = DeploymentCheckpoints(releases.hf.checkpoint_sha256, releases.chf.checkpoint_sha256)
    namespace = "nmrpeak-" + deployment
    projection = _contained_binding_projection(docker, images.provider, definition, policy.binding_name,
                                               namespace, input_sources={})
    if projection["provider_ref"] != selection.provider_ref:
        raise DeploymentOperationRejected("Selected API binding belongs to another named provider generation")
    if policy.runner.ready_seconds > 600:
        raise DeploymentOperationRejected("Runner ready timeout exceeds the Compose health start window")
    state_root = root / "secrets/deployments" / deployment
    runtime_files = {"bindings.toml": definition, **_capture_interpreter_inputs(state_root)}
    for path in projection["input_paths"]:
        leaf = api_input_target(path).relative_to(CONFIG_PATH.parent).as_posix()
        runtime_files[leaf] = _read_capture_source(Path(path), secret=path == projection["credential_file"])
    environment = _compose_environment(
        root, deployment, selection.provider_ref, images, checkpoints,
        provider_config=state_root / "runtime-configs/pending/provider.toml",
        frozen_generation=state_root / "generations/pending/frozen",
    )
    provisional = _render_compose(root, deployment, environment, docker, binding_projection=projection)
    topology = project_deployment_topology(provisional, images, checkpoints, binding_projection=projection)
    generation = render_generation(
        selection, releases, hf_image_input_id=images.hf_input, chf_image_input_id=images.chf_input,
        hf_hello=_read_regular_file(root / "models/nmrpeak_hf_v1/provider/hello.txt"),
        chf_hello=_read_regular_file(root / "models/nmrpeak_chf_v1/provider/hello.txt"), topology=topology,
    )
    runtime_files["provider.toml"] = render_provider_runtime_config(
        policy, frozen_generation_id=generation.frozen_generation_id,
        api_proxy_socket=projection["api_route"]["client_socket"],
    )
    identity = runtime_config_id(runtime_files)
    runtime_root = state_root / "runtime-configs" / _retained_identity_name(identity)
    sources = {path: str(runtime_root / api_input_target(path).relative_to(CONFIG_PATH.parent))
               for path in projection["input_paths"]}
    # Both passes use the same captured definition bytes. The second only
    # relocates selected route inputs to their immutable retained destinations.
    projection = _contained_binding_projection(docker, images.provider, definition, policy.binding_name,
                                               namespace, input_sources=sources)
    validated = _validate_captured_runtime(docker, images.provider, runtime_files)
    expected = {"provider_ref": selection.provider_ref, "api_origin": projection["api_origin"],
                "api_topology": projection["api_topology"], "journal_authority_id": projection["journal_authority_id"],
                "frozen_generation_id": generation.frozen_generation_id,
                "proxy_socket": projection["api_route"]["client_socket"]}
    if validated != expected:
        raise DeploymentOperationRejected("Captured runtime admission differs from its selected binding and frozen generation")
    environment = _compose_environment(
        root, deployment, selection.provider_ref, images, checkpoints,
        provider_config=runtime_root / "provider.toml",
        frozen_generation=state_root / "generations" / _retained_identity_name(generation.frozen_generation_id) / "frozen",
    )
    compose = _render_compose(root, deployment, environment, docker, binding_projection=projection)
    if project_deployment_topology(compose, images, checkpoints, binding_projection=projection) != topology:
        raise DeploymentOperationRejected("Final operational grants changed the frozen scientific topology")
    return DeploymentPlan(compose, generation, releases, selection.provider_ref, identity, runtime_files, projection)


def deployment_plan_bytes(plan: DeploymentPlan) -> bytes:
    """Expose every secret-free generated artifact in one canonical preview."""

    if type(plan) is not DeploymentPlan:
        raise TypeError("Deployment preview requires one resolved plan")
    try:
        provider_config = plan.runtime_files["provider.toml"].decode("utf-8")
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
                "runtime_files": [{"path": path, "byte_length": len(raw),
                                   "sha256": "sha256:" + sha256(raw).hexdigest()}
                                  for path, raw in sorted(plan.runtime_files.items())],
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
    expected_plan_sha256: str,
    docker: Path = _DOCKER,
) -> DeploymentPlan:
    """Start one exact plan and prove all three durable services are ready."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    if (
        type(expected_plan_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256) is None
    ):
        raise DeploymentOperationRejected(
            "Expected deployment plan SHA-256 must be 64 lowercase hexadecimal characters"
        )
    state_root = _ensure_deployment_state_root(root, deployment)
    with _locked_deployment_state(state_root):
        plan = render_deployment_plan(
            root,
            deployment,
            docker=docker,
        )
        _validate_deployment_plan(plan)
        # Match the exact newline-terminated bytes printed by `config`.
        actual_plan_sha256 = sha256(deployment_plan_bytes(plan) + b"\n").hexdigest()
        if actual_plan_sha256 != expected_plan_sha256:
            raise DeploymentOperationRejected(
                "Deployment plan differs from reviewed preview: "
                f"expected SHA-256 {expected_plan_sha256}, "
                f"rendered SHA-256 {actual_plan_sha256}"
            )
        services = _inspect_project_containers(docker, root, deployment)
        _admit_session_volumes(docker, deployment, services)
        _materialize_locked(state_root, plan)
        verify_hf_checkpoint(root, plan.releases.hf, docker_binary=docker)
        verify_chf_checkpoint(root, plan.releases.chf, docker_binary=docker)
        ensure_provider_state_volumes(
            docker,
            root,
            deployment,
            plan.provider_ref,
            plan.binding_projection["journal_authority_id"],
        )
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
    for service in ("provider", "hf-runner", "chf-runner", "api", "api-init"):
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
        image = _resolve_provider_image(docker, root)
        _policy, _definition, binding = _journal_binding(root, deployment, docker, image,
                                      provider_ref, namespace="nmrpeak-" + deployment)
        lock = inspect_provider_identity_lock_volume(docker, provider_ref)
        with _held_provider_identity_lock(docker, lock, provider_ref, image):
            references = _journal_generation_ids(
                docker, root, deployment, provider_ref, binding["journal_authority_id"], services,
            )
            if frozen_generation in references:
                raise DeploymentOperationRejected(
                    "Provider journal still references the frozen generation"
                )
            _remove_retained_generation(state_root, frozen_generation)


def remove_runtime_config(
    repository: Path, deployment: str, runtime_config: str, confirmation: str,
    *, docker: Path = _DOCKER,
) -> None:
    """Remove a nonselected snapshot while cooperating operations are excluded.

    Docker and filesystem administrators must not add borrowers during removal.
    """
    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected("Deployment repository must be one resolved directory")
    _require_deployment_name(deployment)
    if type(runtime_config) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_config) is None:
        raise DeploymentOperationRejected("Runtime config removal requires one full SHA-256 identity")
    if confirmation != runtime_config:
        raise DeploymentOperationRejected("Runtime config removal confirmation must equal its full identity")
    for parent in (root / "secrets", root / "secrets/deployments"):
        metadata = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise DeploymentOperationRejected("Deployment state ancestors must be operator-owned mode 0700 directories")
    state_root = _existing_deployment_state_root(root, deployment)
    with _locked_deployment_state(state_root):
        services = _inspect_project_containers(docker, root, deployment)
        _require_stopped_services(services, "Runtime config removal")
        plan = render_deployment_plan(root, deployment, docker=docker)
        if runtime_config == plan.runtime_config_id:
            raise DeploymentOperationRejected("The selected runtime config cannot be removed")
        image = _resolve_provider_image(docker, root)
        lock = inspect_provider_identity_lock_volume(docker, plan.provider_ref)
        target = state_root / "runtime-configs" / _retained_identity_name(runtime_config)
        with _held_provider_identity_lock(docker, lock, plan.provider_ref, image):
            _require_snapshot_unmounted(docker, target)
            _remove_retained_runtime_config(target, runtime_config)


def _require_snapshot_unmounted(docker: Path, target: Path) -> None:
    """Reject any default-engine container retaining an overlapping host mount."""
    identifiers = _docker_command(docker, ("ps", "--all", "--no-trunc", "--quiet"), timeout=60).stdout.decode("ascii").splitlines()
    if len(identifiers) != len(set(identifiers)) or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in identifiers):
        raise DeploymentOperationRejected("Docker returned an invalid container inventory")
    records = _json_output(_docker_command(docker, ("inspect", *identifiers), timeout=60).stdout, "Snapshot borrower inventory") if identifiers else []
    if type(records) is not list or len(records) != len(identifiers):
        raise DeploymentOperationRejected("Docker returned an incomplete snapshot borrower inventory")
    seen = set()
    volumes = set()
    for record in records:
        if type(record) is not dict or record.get("Id") not in identifiers or record["Id"] in seen or type(record.get("Mounts")) is not list:
            raise DeploymentOperationRejected("Docker returned an invalid snapshot borrower inventory")
        seen.add(record["Id"])
        for mount in record["Mounts"]:
            if type(mount) is not dict:
                raise DeploymentOperationRejected("Docker returned an invalid container mount")
            if mount.get("Type") not in {"bind", "volume"}:
                continue
            source = mount.get("Source")
            if type(source) is not str or not Path(source).is_absolute():
                raise DeploymentOperationRejected("Docker returned an invalid container mount source")
            path = Path(source).resolve()
            if path == target or path in target.parents or target in path.parents:
                raise DeploymentOperationRejected("A container still mounts the retained runtime config")
            if mount["Type"] == "volume":
                name = mount.get("Name")
                if type(name) is not str or not name:
                    raise DeploymentOperationRejected("Docker returned an invalid container volume name")
                volumes.add(name)
    volume_names = _docker_command(docker, ("volume", "ls", "--quiet"), timeout=60).stdout.decode("utf-8").splitlines()
    if (len(volume_names) != len(set(volume_names))
            or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is None for name in volume_names)
            or not volumes.issubset(volume_names)):
        raise DeploymentOperationRejected("Docker returned an invalid volume inventory")
    if volume_names:
        _require_volume_sources_unmounted(docker, set(volume_names), target)


def _require_volume_sources_unmounted(docker: Path, names: set[str], target: Path) -> None:
    output = _docker_command(docker, ("volume", "inspect", *sorted(names)), timeout=60).stdout
    records = _json_output(output, "Snapshot volume borrower inventory")
    if type(records) is not list or len(records) != len(names):
        raise DeploymentOperationRejected("Docker returned an incomplete snapshot volume inventory")
    seen = set()
    for record in records:
        if type(record) is not dict or record.get("Name") not in names or record["Name"] in seen:
            raise DeploymentOperationRejected("Docker returned an invalid snapshot volume inventory")
        seen.add(record["Name"])
        if record.get("Driver") != "local":
            raise DeploymentOperationRejected("A Docker volume uses an uninspectable nonlocal driver")
        options = record.get("Options")
        if options is None:
            options = {}
        if type(options) is not dict or any(type(k) is not str or type(v) is not str for k, v in options.items()):
            raise DeploymentOperationRejected("Docker returned invalid local volume options")
        mount_options = options.get("o", "").split(",")
        if "bind" in mount_options or "rbind" in mount_options:
            device = options.get("device")
            if type(device) is not str or not Path(device).is_absolute():
                raise DeploymentOperationRejected("A bind-backed volume has an invalid host source")
            source = Path(device).resolve()
            if source == target or source in target.parents or target in source.parents:
                raise DeploymentOperationRejected("A Docker volume still references the retained runtime config")


def _remove_retained_runtime_config(target: Path, identity: str) -> None:
    parent = target.parent
    metadata = parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise DeploymentOperationRejected("Retained runtime config directory must be operator-owned mode 0700")
    if target.is_symlink() or not target.is_dir():
        raise DeploymentOperationRejected("Retained runtime config is not a non-symlink directory")
    files = {}
    total = entries = 0
    pending = [target]
    while pending:
        current = pending.pop()
        with os.scandir(current) as directory:
            for entry in directory:
                entries += 1
                if entries > 512:
                    raise DeploymentOperationRejected("Retained runtime config contains too many entries")
                path = current / entry.name
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    content = _read_regular_file(path)
                    total += len(content)
                    if total > 8 * 1024 * 1024:
                        raise DeploymentOperationRejected("Retained runtime config exceeds its inventory byte limit")
                    files[path.relative_to(target).as_posix()] = content
                else:
                    raise DeploymentOperationRejected("Retained runtime config contains a non-regular entry")
    _require_retained_tree(target, files)
    if runtime_config_id(files) != identity:
        raise DeploymentOperationRejected("Retained runtime config identity does not match its files")
    staged = parent / f".{target.name}.{secrets.token_hex(16)}.removing"
    renamed = deleted = False
    try:
        os.rename(target, staged)
        renamed = True
        _fsync_directory(parent)
        shutil.rmtree(staged)
        deleted = True
        _fsync_directory(parent)
    except OSError as error:
        if deleted:
            raise DeploymentOperationRejected("Runtime config was removed, but deletion durability is unconfirmed") from error
        if renamed:
            raise DeploymentOperationRejected("Runtime config removal was incomplete; staged residue remains") from error
        raise DeploymentOperationRejected("Runtime config could not be staged for removal") from error


def inspect_provider_journal(
    repository: Path,
    deployment: str,
    *,
    docker: Path = _DOCKER,
    execution_attempt_ref: str | None = None,
) -> bytes:
    """Inspect retained work offline while deployment and provider locks are held."""
    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected("Deployment repository must be one resolved directory")
    _require_deployment_name(deployment)
    state_root = _existing_deployment_state_root(root, deployment)
    with _locked_deployment_state(state_root):
        services = _inspect_project_containers(docker, root, deployment)
        try:
            _require_stopped_services(services, "Provider journal inspection")
        except DeploymentOperationRejected as error:
            raise DeploymentOperationRejected(
                f"{error}. Run make provider/deployment/down DEPLOYMENT={deployment} before inspection."
            ) from error
        selection = load_named_deployment(
            root / "config/deployments" / deployment / "deployment.toml"
        )
        image = _resolve_provider_image(docker, root)
        _policy, _definition, binding = _journal_binding(root, deployment, docker, image,
                                  selection.provider_ref, namespace="nmrpeak-" + deployment)
        journal = _admitted_journal_volume(
            docker, deployment, selection.provider_ref, binding["journal_authority_id"], services,
        )
        lock = inspect_provider_identity_lock_volume(docker, selection.provider_ref)
        with _held_provider_identity_lock(docker, lock, selection.provider_ref, image):
            arguments = ("--execution-attempt-ref", execution_attempt_ref) if execution_attempt_ref else ()
            output = _run_journal_reader(
                docker, journal, image, "nmrpeak_provider.journal_inspect", *arguments
            )
        try:
            if not output.endswith(b"\n"):
                raise ValueError("Missing newline")
            document = parse_canonical_json_bytes(output[:-1])
            validate_inspection_document(document)
        except (TypeError, ValueError) as error:
            raise DeploymentOperationRejected("Provider journal inspection returned invalid output") from error
        return output


def _run_journal_api_client(docker, image, projection, captured, module, arguments, *, mounts=()):
    """Run one bounded network-isolated reader with the SDK's captured client grant."""
    route = projection["api_route"]
    client = route["client_mount"]
    volume = route["compose"]["volumes"][client["source"]]["name"]
    token = secrets.token_hex(16)
    name = "nmrpeak-journal-archive-" + token
    grants = ("--mount", f"type=bind,src={captured},dst={CONFIG_PATH.parent},readonly",
              "--mount", f"type=volume,src={volume},dst={client['target']},readonly,volume-nocopy")
    try:
        return _docker_command(docker, (
            "run", "--rm", "--name", name, "--label", "io.nmrpeak.archive=" + token,
            "--pull", "never", "--network", "none", "--read-only", "--user", "65532:65532",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "32",
            "--memory", "128m", "--memory-swap", "128m", "--log-driver", "none",
            "--env", "PROVIDER_CORE_IN_CONTAINER=1", *grants, *mounts,
            "--entrypoint", "/usr/local/bin/python3.12", image.image_id,
            "-I", "-B", "-m", module, *arguments,
        ), timeout=60).stdout
    except BaseException as original:
        try:
            _remove_archive_invocation(docker, name, token)
        except BaseException as cleanup:
            raise DeploymentOperationRejected(
                f"Journal maintenance result and cleanup are unconfirmed; preserve the journal and inspect name={name}, label=io.nmrpeak.archive={token} before retrying"
            ) from BaseExceptionGroup("Maintenance invocation and cleanup failed", [original, cleanup])
        raise DeploymentOperationRejected(
            "Journal maintenance result is unconfirmed; invocation stopped. Preserve and inspect the journal and archive before retrying"
        ) from original


def archive_provider_journal(repository: Path, deployment: str, *, execution_attempt_ref: str,
                             record_digest: str, reason: str, frozen_generation: str,
                             docker: Path = _DOCKER) -> bytes:
    """Retire one reviewed held record using its authenticated retained generation."""
    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected("Deployment repository must be one resolved directory")
    _require_deployment_name(deployment)
    validate_frozen_generation_id(frozen_generation)
    state_root = _existing_deployment_state_root(root, deployment)
    with _locked_deployment_state(state_root):
        services = _inspect_project_containers(docker, root, deployment)
        _require_stopped_services(services, "Provider journal archival")
        selection = load_named_deployment(root / "config/deployments" / deployment / "deployment.toml")
        image = _resolve_provider_image(docker, root)
        _policy, _definition, binding = _journal_binding(root, deployment, docker, image,
                                  selection.provider_ref, namespace="nmrpeak-" + deployment)
        authority = binding["journal_authority_id"]
        journal = _admitted_journal_volume(docker, deployment, selection.provider_ref, authority, services)
        lock = inspect_provider_identity_lock_volume(docker, selection.provider_ref)
        frozen_root = state_root / "generations" / _retained_identity_name(frozen_generation) / "frozen"
        if frozen_root.resolve(strict=True) != frozen_root:
            raise DeploymentOperationRejected("Archive generation must be a retained non-symlink directory")
        frozen = load_frozen_generation(frozen_root, expected_frozen_generation_id=frozen_generation)
        if frozen.runtime.hf.generation.provider_ref != selection.provider_ref:
            raise DeploymentOperationRejected("Archive frozen generation belongs to another provider")
        mounts = ("--mount", f"type=volume,src={journal},dst=/var/lib/nmrpeak-provider",
                  "--mount", f"type=volume,src={lock},dst=/run/nmrpeak-provider-lock,readonly",
                  "--mount", f"type=bind,src={frozen_root},dst=/run/nmrpeak-provider/frozen,readonly")
        with _held_maintenance_api_route(docker, root, deployment, image, selection.provider_ref, authority) as (policy, projection, captured):
            output = _run_journal_api_client(docker, image, projection, captured,
                "nmrpeak_provider.journal_archive", (
                    "--execution-attempt-ref", execution_attempt_ref, "--record-digest", record_digest,
                    "--reason", reason, "--frozen-generation", frozen_generation,
                    "--expected-provider-ref", selection.provider_ref, "--expected-authority-id", authority,
                    "--binding", policy.binding_name, "--proxy-socket", projection["api_route"]["client_socket"],
                ), mounts=mounts)
        try:
            if len(output) > 16385 or not output.endswith(b"\n"):
                raise ValueError("Invalid framing")
            document = validate_archive_document(parse_canonical_json_bytes(output[:-1]))
            if document["execution_attempt_ref"] != execution_attempt_ref or document["record_digest"] != record_digest:
                raise ValueError("Selection mismatch")
        except (TypeError, ValueError) as error:
            raise DeploymentOperationRejected("Archive returned invalid output; inspect journal and archive before retrying") from error
        return output


def _remove_archive_invocation(docker: Path, name: str, token: str) -> None:
    """Stop only the inspected invocation when Docker observation failed."""
    identifiers = _docker_command(docker, ("ps", "-a", "--no-trunc", "--filter",
                    "name=^/" + name + "$", "--format", "{{.ID}}"), timeout=30).stdout.decode("ascii").splitlines()
    if not identifiers:
        return
    if len(identifiers) != 1 or re.fullmatch(r"[0-9a-f]{64}", identifiers[0]) is None:
        raise DeploymentOperationRejected("Archive container inventory is malformed")
    identifier = identifiers[0]
    records = _json_output(_docker_command(docker, ("inspect", identifier), timeout=30).stdout, "Archive cleanup")
    if type(records) is not list or len(records) != 1 or type(records[0]) is not dict:
        raise DeploymentOperationRejected("Archive container inspection is malformed")
    record = records[0]
    config = record.get("Config")
    labels = config.get("Labels") if type(config) is dict else None
    if (record.get("Id") != identifier or record.get("Name") != "/" + name
            or type(labels) is not dict or labels.get("io.nmrpeak.archive") != token):
        raise DeploymentOperationRejected("Archive container ownership does not match this invocation")
    _docker_command(docker, ("rm", "-f", identifier), timeout=30)


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
        provider_image = _resolve_provider_image(docker, root)
        _policy, _definition, binding = _journal_binding(root, deployment, docker, provider_image,
                                   selection.provider_ref, namespace="nmrpeak-" + deployment)
        authority_id = binding["journal_authority_id"]
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
        with _held_provider_identity_lock(
            docker,
            lock_volume,
            selection.provider_ref,
            provider_image,
        ):
            with _held_maintenance_api_route(docker, root, deployment, provider_image,
                      selection.provider_ref, authority_id) as (policy, projection, captured):
                output = _run_journal_api_client(docker, provider_image, projection, captured,
                    "nmrpeak_provider.journal_api", (
                        "--binding", policy.binding_name, "--proxy-socket", projection["api_route"]["client_socket"],
                        "--expected-provider-ref", selection.provider_ref,
                        "--expected-authority-id", authority_id,
                        "--maximum-pages", str(policy.process.inventory_maximum_pages),
                    ))
                if output != b"complete-empty\n":
                    raise DeploymentOperationRejected("Provider journal retirement did not prove a complete empty Attempt inventory")
            # Teardown must also succeed before the parent-held lock permits deletion.
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
        route_volumes = _api_route_volumes(deployment, services)
        _inspect_project_resources(docker, deployment, services, route_volumes=route_volumes)
        _run_compose_down(docker, root, deployment, route_volumes=route_volumes)
        remaining = _inspect_project_containers(docker, root, deployment)
        resources = _inspect_project_resources(docker, deployment, remaining, route_volumes=route_volumes)
        if remaining or resources:
            raise DeploymentOperationRejected(
                "Provider teardown left unresolved project resources"
            )


def _validate_deployment_plan(plan: DeploymentPlan) -> None:
    if type(plan) is not DeploymentPlan:
        raise TypeError("Deployment materialization requires one resolved plan")
    if runtime_config_id(plan.runtime_files) != plan.runtime_config_id:
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
        plan.runtime_files,
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
                f"Provider identity-lock holder did not acquire the engine-global lock; exit={process.poll()}, readiness="
                + diagnostic_text(repr(ready), limit=256)
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
            primary_error.add_note("Identity-lock cleanup also failed: " + exception_diagnostic(cleanup_error))
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
            container_error.add_note("Docker client reap also failed: " + exception_diagnostic(client_error))
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


def _admitted_journal_volume(
    docker: Path,
    deployment: str,
    provider_ref: str,
    authority_id: str,
    services: dict[str, dict[str, object]],
) -> str:
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
    return journal_volume


def _run_journal_reader(
    docker: Path, journal_volume: str, provider_image: LocalImage, module: str,
    *arguments: str,
) -> bytes:
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
            module,
            *arguments,
        ),
        timeout=300,
    )
    return result.stdout


def _journal_generation_ids(
    docker: Path,
    repository: Path,
    deployment: str,
    provider_ref: str,
    authority_id: str,
    services: dict[str, dict[str, object]],
) -> tuple[str, ...]:
    journal_volume = _admitted_journal_volume(
        docker, deployment, provider_ref, authority_id, services
    )
    output = _run_journal_reader(
        docker, journal_volume, _resolve_provider_image(docker, repository),
        "nmrpeak_provider.journal_inventory",
    )
    if not output.endswith(b"\n") or output.endswith(b"\n\n"):
        raise DeploymentOperationRejected(
            "Provider journal inventory returned invalid framing"
        )
    try:
        document = parse_canonical_json_bytes(output[:-1])
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
            f"Cannot confirm provider deployment {deployment!r} is ready because "
            "Docker Compose did not report all required services ready. Containers and "
            "session volumes may remain. Run `make provider/deployment/status "
            f"DEPLOYMENT={deployment}` and `make provider/logs DEPLOYMENT={deployment}` "
            "before deciding whether to stop or retry."
        ) from error


def _run_compose_down(
    docker: Path,
    repository: Path,
    deployment: str,
    *,
    route_volumes: tuple[str, ...],
    api_route=None,
) -> None:
    try:
        teardown = canonical_json_bytes(api_route) if api_route is not None else _read_committed_template(
            repository,
            Path("compose/provider-teardown.yml"),
        )
        with TemporaryDirectory() as temporary:
            compose = Path(temporary) / "provider-teardown.yml"
            compose.write_bytes(teardown)
            route_overlay = Path(temporary) / "route-volumes.json"
            route_overlay.write_bytes(canonical_json_bytes({
                "services": {role: {"volumes": [{"type": "volume", "source": name, "target": "/route"}
                                                for name in route_volumes]}
                             for role in ("api", "api-init")},
                "volumes": {name: {"name": name} for name in route_volumes},
            }))
            prefix = (
                "compose",
                "--env-file",
                "/dev/null",
                "--project-directory",
                str(repository),
                "--project-name",
                f"nmrpeak-{deployment}",
                "--file",
                str(compose), "--file", str(route_overlay),
            )
            if api_route is None:
                _docker_command(
                    docker, (*prefix, "stop", "--timeout", "600", "provider"), timeout=660,
                )
                _docker_command(
                    docker, (*prefix, "stop", "--timeout", "20", "hf-runner", "chf-runner"), timeout=80,
                )
            _docker_command(
                docker,
                (
                    *prefix,
                    "down",
                    "--timeout",
                    "20",
                    "--volumes",
                ),
                timeout=80,
            )
    except DeploymentOperationRejected as error:
        raise DeploymentOperationRejected(
            f"Cannot confirm provider deployment {deployment!r} is stopped because "
            "Docker Compose did not complete teardown. Containers, the project network, "
            "or session volumes may remain. Run `make provider/deployment/status "
            f"DEPLOYMENT={deployment}` before retrying shutdown."
        ) from error


def _inspect_project_containers(
    docker: Path,
    repository: Path,
    deployment: str,
    *,
    roles=frozenset({"provider", "hf-runner", "chf-runner", "api", "api-init"}),
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
            or service not in roles
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


def _api_route_volumes(deployment, services):
    """Retain original SDK role mount evidence for this teardown transaction."""
    names = set()
    bindings = set()
    for role, sdk_role in (("api", "proxy"), ("api-init", "initializer")):
        if role not in services:
            continue
        record = services[role]
        labels = record["Config"]["Labels"]
        binding = labels.get("org.nmr-provider-tools.binding")
        if (labels.get("org.nmr-provider-tools.route") != "nmrpeak-" + deployment
                or labels.get("org.nmr-provider-tools.route-role") != sdk_role
                or type(binding) is not str or not binding):
            raise DeploymentOperationRejected("API route container does not retain its original SDK ownership labels")
        mounts = record.get("Mounts")
        if type(mounts) is not list:
            raise DeploymentOperationRejected("API route container has no inspectable original mounts")
        if any(type(mount) is not dict for mount in mounts):
            raise DeploymentOperationRejected("API route container mount inspection contains an invalid record")
        selected = [mount for mount in mounts if mount.get("Destination") == "/route"]
        if (len(selected) != 1 or selected[0].get("Type") != "volume"
                or selected[0].get("RW") is not True
                or type(selected[0].get("Name")) is not str or not selected[0]["Name"]):
            raise DeploymentOperationRejected("API route container does not grant exactly one writable route volume")
        names.add(selected[0]["Name"])
        bindings.add(binding)
    if len(names) > 1 or len(bindings) > 1:
        raise DeploymentOperationRejected("API proxy and initializer retain different original route grants")
    return tuple(sorted(names))


def _admit_session_volumes(docker: Path, deployment: str, services: dict[str, dict[str, object]]) -> None:
    """Refuse reused session volumes whose actual grant differs from Compose."""
    project = f"nmrpeak-{deployment}"
    names = _docker_command(docker, ("volume", "ls", "--quiet"), timeout=60).stdout.decode("ascii").splitlines()
    if len(names) != len(set(names)):
        raise DeploymentOperationRejected("Docker returned an ambiguous volume inventory")
    for lane in ("hf", "chf"):
        logical = f"{lane}-session"
        name = f"{project}_{logical}"
        if name not in names:
            continue
        records = _json_output(_docker_command(docker, ("volume", "inspect", name), timeout=60).stdout, "Session volume inspection")
        if type(records) is not list or len(records) != 1 or type(records[0]) is not dict:
            raise DeploymentOperationRejected("Docker returned an invalid session volume inspection")
        record = records[0]
        labels = record.get("Labels")
        if (record.get("Name") != name or record.get("Driver") != "local"
                or record.get("Options") != _SESSION_VOLUME_OPTIONS or type(labels) is not dict
                or labels.get("com.docker.compose.project") != project
                or labels.get("com.docker.compose.volume") != logical):
            raise DeploymentOperationRejected(f"Session volume {name} has different ownership, driver, or options")
        attachments = _docker_command(docker, ("ps", "--all", "--no-trunc", "--quiet", "--filter", f"volume={name}"), timeout=60).stdout.decode("ascii").splitlines()
        allowed = {services[role]["Id"]: (role, destination) for role, destination in (
            ("provider", f"/run/nmrpeak-provider/{lane}"), (f"{lane}-runner", "/run/nmrpeak"),
        ) if role in services}
        if len(attachments) != len(set(attachments)) or any(re.fullmatch(r"[0-9a-f]{64}", value) is None or value not in allowed for value in attachments):
            raise DeploymentOperationRejected(f"Session volume {name} has a foreign container attachment")
        for identifier in attachments:
            role, destination = allowed[identifier]
            mounts = services[role].get("Mounts")
            if type(mounts) is not list or any(type(mount) is not dict for mount in mounts):
                raise DeploymentOperationRejected(f"Session volume {name} has an uninspectable container grant")
            grants = [mount for mount in mounts if mount.get("Type") == "volume" and mount.get("Name") == name]
            if len(grants) != 1 or grants[0].get("Destination") != destination or grants[0].get("RW") is not True:
                raise DeploymentOperationRejected(f"Session volume {name} has an unexpected container grant")


def _inspect_project_resources(
    docker: Path,
    deployment: str,
    services: dict[str, dict[str, object]],
    *,
    route_volumes: tuple[str, ...],
    scientific_sessions=True,
) -> tuple[str, ...]:
    project = f"nmrpeak-{deployment}"
    project_container_ids = {record["Id"] for record in services.values()}
    expected_volumes = {
        f"{project}_hf-session": "hf-session",
        f"{project}_chf-session": "chf-session",
    } if scientific_sessions else {}
    if set(route_volumes) & expected_volumes.keys():
        raise DeploymentOperationRejected("An API route reuses a scientific session volume")
    expected_volumes.update({name: name for name in route_volumes})
    route_container_ids = {record["Id"] for role, record in services.items()
                           if role in {"provider", "api", "api-init"}}
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
            options = record.get("Options")
            if name in route_volumes:
                if options not in (None, {}):
                    raise DeploymentOperationRejected("API route volume has unexpected driver options")
            elif options != _SESSION_VOLUME_OPTIONS:
                raise DeploymentOperationRejected("Scientific session volume options have drifted")
            if (
                name not in volume_names
                or type(labels) is not dict
                or record.get("Driver") != "local"
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
            ) or not set(attachments).issubset(route_container_ids if name in route_volumes else project_container_ids):
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
    if set(services) != set(expected_services):
        raise DeploymentOperationRejected(
            "Provider startup did not leave the exact declared service roles"
        )
    for service, record in services.items():
        expected = expected_services[service]
        state = record.get("State")
        if type(expected) is not dict or type(state) is not dict or record.get("Image") != expected.get("image"):
            raise DeploymentOperationRejected(f"Provider startup did not prove the selected {service} image")
        if service == "api-init":
            if state.get("Status") != "exited" or state.get("ExitCode") != 0:
                raise DeploymentOperationRejected("API route initializer did not finish successfully")
            continue
        if state.get("Status") != "running":
            raise DeploymentOperationRejected(f"Provider startup did not prove the selected {service} image running")
        if service in {"provider", "api"}:
            health = state.get("Health")
            if type(health) is not dict or health.get("Status") != "healthy":
                raise DeploymentOperationRejected(
                    f"Provider startup did not prove {service} readiness"
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
    except OSError as error:
        raise DeploymentOperationRejected(
            "Cannot run the Docker provider deployment operation: the Docker client "
            "could not start"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise DeploymentOperationRejected(
            f"The Docker provider deployment operation did not finish within {timeout} "
            "seconds; its effects are unknown. Partial stderr: "
            + (_bounded_docker_diagnostic(error.stderr or b"") or "none")
        ) from error
    if len(result.stdout) > 1_048_576 or len(result.stderr) > 1_048_576:
        raise DeploymentOperationRejected(
            "The Docker provider deployment operation produced more than 1048576 bytes "
            f"of output (stdout={len(result.stdout)}, stderr={len(result.stderr)}), so its result was not accepted: "
            + (_bounded_docker_diagnostic(result.stderr) or "no stderr")
        )
    if result.returncode != 0:
        diagnostic = _bounded_docker_diagnostic(result.stderr)
        detail = f": {diagnostic}" if diagnostic is not None else ""
        raise DeploymentOperationRejected(
            "The Docker provider deployment operation exited with status "
            f"{result.returncode}{detail}"
        )
    return result


def _bounded_docker_diagnostic(raw: bytes) -> str | None:
    """Retain bounded stderr after credential and terminal-control removal."""
    text = diagnostic_text(_ANSI_ESCAPE.sub("", raw.decode("utf-8", errors="replace")), limit=4096)
    return text or None


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
            f"Provider input access operation {executable.name} for {arguments[-1]} "
            + ("timed out; effects are unknown: " if isinstance(error, subprocess.TimeoutExpired) else "could not start: ")
            + exception_diagnostic(error)
            + (" Partial stderr: " + (_bounded_docker_diagnostic(error.stderr or b"") or "none") if isinstance(error, subprocess.TimeoutExpired) else "")
        ) from error
    if result.returncode != 0 or len(result.stdout) > 65_536 or result.stderr:
        raise DeploymentOperationRejected(
            f"Provider input access operation {executable.name} for {arguments[-1]} was rejected; exit={result.returncode}, "
            f"stdout_bytes={len(result.stdout)}, stderr_bytes={len(result.stderr)}: "
            + (_bounded_docker_diagnostic(result.stderr) or "no stderr")
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
        "PROVIDER_RUNTIME_DIRECTORY": str(provider_config.parent),
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
    return environment


def _render_compose(
    repository: Path,
    deployment: str,
    environment: dict[str, str],
    docker: Path,
    *,
    binding_projection: dict,
) -> dict[str, object]:
    project = f"nmrpeak-{deployment}"
    compose_files = [
        "--file",
        str(repository / "compose/provider.yml"),
    ]
    try:
        with TemporaryDirectory() as temporary:
            overlay = Path(temporary) / "api-route.json"
            fragment = _compose_binding_route({"services": {"provider": {}}, "networks": {}, "volumes": {}}, binding_projection)
            overlay.write_bytes(canonical_json_bytes(fragment))
            compose_files.extend(("--file", str(overlay)))
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
            "Docker Compose could not normalize the deployment topology: " + exception_diagnostic(error)
            + (" Partial stderr: " + (_bounded_docker_diagnostic(error.stderr or b"") or "none") if isinstance(error, subprocess.TimeoutExpired) else "")
        ) from error
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 1_048_576:
        raise DeploymentOperationRejected(
            f"Docker Compose rejected the deployment topology; exit={result.returncode}, stdout_bytes={len(result.stdout)}: "
            + (_bounded_docker_diagnostic(result.stderr) or "no stderr")
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
            f"Git could not inspect the deployment checkout; exit={result.returncode}: "
            + (_bounded_docker_diagnostic(result.stderr) or "no stderr")
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
    parser = argparse.ArgumentParser(description="Operate one named provider deployment.")
    parser.add_argument(
        "operation",
        choices=(
            "config",
            "down",
            "generation-remove",
            "runtime-config-remove",
            "init",
            "journal-inspect",
            "journal-archive-closed",
            "journal-retire",
            "logs",
            "status",
            "up",
        ),
    )
    parser.add_argument("deployment")
    parser.add_argument("--frozen-generation")
    parser.add_argument("--runtime-config")
    parser.add_argument("--confirm")
    parser.add_argument("--execution-attempt-ref")
    parser.add_argument("--record-digest")
    parser.add_argument("--reason")
    parser.add_argument("--expected-plan-sha256")
    options = parser.parse_args(arguments)
    _validate_operation_options(parser, options)
    repository = Path(__file__).resolve().parents[1]
    try:
        if options.operation == "runtime-config-remove":
            remove_runtime_config(repository, options.deployment, options.runtime_config, options.confirm)
            print(f"Removed retained runtime config: {options.deployment} {options.runtime_config}")
        elif options.operation == "generation-remove":
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
        elif options.operation == "journal-inspect":
            print(inspect_provider_journal(repository, options.deployment,
                  execution_attempt_ref=options.execution_attempt_ref).decode("utf-8"), end="")
        elif options.operation == "journal-archive-closed":
            print(archive_provider_journal(repository, options.deployment,
                  execution_attempt_ref=options.execution_attempt_ref, record_digest=options.record_digest,
                  reason=options.reason, frozen_generation=options.frozen_generation).decode("utf-8"), end="")
        elif options.operation == "journal-retire":
            removed = retire_provider_journal(
                repository,
                options.deployment,
                options.confirm,
            )
            print(
                f"Retired provider journal volume: {removed}. "
                "Retained commands, archived results and evidence in this volume were deleted. "
                "Docker volume deletion is not secure erasure of underlying storage."
            )
        elif options.operation == "init":
            initialized = initialize_deployment(repository, options.deployment)
            print(f"Initialized deployment {options.deployment}: {initialized}")
        elif options.operation == "config":
            plan = render_deployment_plan(
                repository,
                options.deployment,
            )
            print(deployment_plan_bytes(plan).decode("utf-8"))
        elif options.operation == "up":
            plan = start_deployment(
                repository,
                options.deployment,
                expected_plan_sha256=options.expected_plan_sha256,
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
            _render_deployment_failure(
                options.operation,
                options.deployment,
                error,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


_OPERATION_OPTION_POLICY = {
    "config": (frozenset(), frozenset()),
    "down": (frozenset(), frozenset()),
    "generation-remove": (
        frozenset({"frozen_generation", "confirm"}),
        frozenset({"frozen_generation", "confirm"}),
    ),
    "runtime-config-remove": (
        frozenset({"runtime_config", "confirm"}),
        frozenset({"runtime_config", "confirm"}),
    ),
    "init": (frozenset(), frozenset()),
    "journal-inspect": (frozenset(), frozenset({"execution_attempt_ref"})),
    "journal-archive-closed": (frozenset({"execution_attempt_ref", "record_digest", "reason", "frozen_generation"}),
        frozenset({"execution_attempt_ref", "record_digest", "reason", "frozen_generation"})),
    "journal-retire": (frozenset({"confirm"}), frozenset({"confirm"})),
    "logs": (frozenset(), frozenset()),
    "status": (frozenset(), frozenset()),
    "up": (frozenset({"expected_plan_sha256"}), frozenset({"expected_plan_sha256"})),
}
_OPTION_NAMES = {
    "frozen_generation": "--frozen-generation",
    "runtime_config": "--runtime-config",
    "confirm": "--confirm",
    "execution_attempt_ref": "--execution-attempt-ref",
    "record_digest": "--record-digest",
    "reason": "--reason",
    "expected_plan_sha256": "--expected-plan-sha256",
}
_OPERATION_FAILURE_HEADLINES = {
    "config": "Provider deployment preview failed",
    "down": "Provider deployment shutdown failed",
    "generation-remove": "Frozen deployment generation removal failed",
    "runtime-config-remove": "Retained runtime config removal failed",
    "init": "Provider deployment initialization failed",
    "journal-inspect": "Provider journal inspection failed",
    "journal-archive-closed": "Provider journal archival failed",
    "journal-retire": "Provider journal retirement failed",
    "logs": "Provider log read failed",
    "status": "Provider deployment status read failed",
    "up": "Provider deployment startup failed",
}


def _validate_operation_options(
    parser: argparse.ArgumentParser,
    options: argparse.Namespace,
) -> None:
    required, allowed = _OPERATION_OPTION_POLICY[options.operation]
    supplied = {
        name
        for name in _OPTION_NAMES
        if getattr(options, name) not in {None, False}
    }
    missing = [name for name in _OPTION_NAMES if name in required - supplied]
    unexpected = [name for name in _OPTION_NAMES if name in supplied - allowed]
    failures = []
    if missing:
        failures.append(
            "requires " + " and ".join(_OPTION_NAMES[name] for name in missing)
        )
    if unexpected:
        failures.append(
            "does not accept "
            + ", ".join(_OPTION_NAMES[name] for name in unexpected)
        )
    if failures:
        parser.error(f"{options.operation} " + "; ".join(failures))


def _render_deployment_failure(
    operation: str,
    deployment: str,
    error: Exception,
) -> str:
    """Render operator context with sanitized causes and cleanup evidence."""
    return (f"{_OPERATION_FAILURE_HEADLINES[operation]} for {deployment!r}: "
            + exception_diagnostic(error, limit=8192))


if __name__ == "__main__":
    raise SystemExit(main())
