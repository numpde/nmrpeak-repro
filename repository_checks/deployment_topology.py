"""Validate and project the fixed normalized production Compose topology."""

from __future__ import annotations

from dataclasses import dataclass
import re

from nmrpeak_provider.canonical_json import JsonValue, canonical_json_bytes


_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
_SERVICES = {"provider", "hf-runner", "chf-runner"}
_VOLUMES = {
    "provider-identity-lock",
    "provider-journal",
    "hf-checkpoint",
    "chf-checkpoint",
    "hf-session",
    "chf-session",
}
_COMMON = {
    "cap_drop",
    "command",
    "cpus",
    "entrypoint",
    "image",
    "init",
    "logging",
    "mem_limit",
    "memswap_limit",
    "pids_limit",
    "platform",
    "pull_policy",
    "read_only",
    "restart",
    "security_opt",
    "stop_grace_period",
    "tmpfs",
    "user",
    "volumes",
}
_PROVIDER_FIELDS = _COMMON | {"healthcheck", "networks"}
_RUNNER_FIELDS = _COMMON | {"network_mode", "shm_size"}


class DeploymentTopologyRejected(ValueError):
    """Normalized Compose grants authority outside the fixed product topology."""


@dataclass(frozen=True, slots=True)
class DeploymentImages:
    """Resolved immutable image and input identities for all three services."""

    provider: str
    provider_input: str
    hf: str
    hf_input: str
    chf: str
    chf_input: str


@dataclass(frozen=True, slots=True)
class DeploymentCheckpoints:
    """Release identities supplied to the two fixed runner commands."""

    hf: str
    chf: str


def project_deployment_topology(
    document: object,
    images: DeploymentImages,
    checkpoints: DeploymentCheckpoints,
    *,
    private_ca: bool = False,
) -> bytes:
    """Return canonical secret-free facts from one admitted Compose document."""

    if (
        type(images) is not DeploymentImages
        or type(checkpoints) is not DeploymentCheckpoints
        or type(private_ca) is not bool
    ):
        raise TypeError("Deployment topology requires admitted image and release facts")
    if not all(
        _is_sha256(value)
        for value in (
            images.provider,
            images.provider_input,
            images.hf,
            images.hf_input,
            images.chf,
            images.chf_input,
            checkpoints.hf,
            checkpoints.chf,
        )
    ):
        raise DeploymentTopologyRejected(
            "Deployment topology identities must be exact SHA-256 references"
        )
    if type(document) is not dict or set(document) != {
        "name",
        "networks",
        "services",
        "volumes",
    }:
        raise DeploymentTopologyRejected("Normalized Compose fields are invalid")
    services = _mapping(document, "services")
    volumes = _mapping(document, "volumes")
    if set(services) != _SERVICES or set(volumes) != _VOLUMES:
        raise DeploymentTopologyRejected(
            "Normalized Compose must contain exactly the fixed services and volumes"
        )
    networks = _mapping(document, "networks")
    if set(networks) != {"default"}:
        raise DeploymentTopologyRejected(
            "Normalized Compose must expose only the provider default network"
        )
    network = networks["default"]
    if type(network) is not dict or set(network) != {"ipam", "name"} or network["ipam"] != {}:
        raise DeploymentTopologyRejected("Provider default network fields are invalid")

    provider_fields = _PROVIDER_FIELDS | ({"extra_hosts"} if private_ca else set())
    provider = _service(services, "provider", provider_fields)
    hf = _service(services, "hf-runner", _RUNNER_FIELDS)
    chf = _service(services, "chf-runner", _RUNNER_FIELDS)
    _common_posture(provider, images.provider, cpus=1, memory=268_435_456, pids=64)
    _common_posture(hf, images.hf, cpus=8, memory=34_359_738_368, pids=256)
    _common_posture(chf, images.chf, cpus=8, memory=34_359_738_368, pids=256)
    _provider_posture(provider, private_ca=private_ca)
    _runner_posture(hf, "hf", checkpoints.hf, images.hf_input)
    _runner_posture(chf, "chf", checkpoints.chf, images.chf_input)
    _volume_posture(volumes)

    projection: dict[str, JsonValue] = {
        "schema_id": "nmrpeak.deployment_topology.v1",
        "services": [
            _project_service("provider", provider, images.provider_input),
            _project_service("hf", hf, images.hf_input),
            _project_service("chf", chf, images.chf_input),
        ],
        "session_volumes": {
            lane: {
                "driver": volumes[f"{lane}-session"]["driver"],
                "driver_opts": volumes[f"{lane}-session"]["driver_opts"],
            }
            for lane in ("hf", "chf")
        },
        "checkpoint_releases": {"hf": checkpoints.hf, "chf": checkpoints.chf},
    }
    return canonical_json_bytes(projection)


def _common_posture(
    service: dict[str, object],
    image: str,
    *,
    cpus: int,
    memory: int,
    pids: int,
) -> None:
    if not _is_sha256(image):
        raise DeploymentTopologyRejected("Deployment image identity is invalid")
    expected = {
        "image": image,
        "platform": "linux/amd64",
        "user": "65532:65532",
        "init": True,
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pull_policy": "never",
        "restart": "no",
        "cpus": cpus,
        "mem_limit": str(memory),
        "memswap_limit": str(memory),
        "pids_limit": pids,
    }
    if any(service.get(name) != value for name, value in expected.items()):
        raise DeploymentTopologyRejected("Deployment service posture has drifted")


def _provider_posture(service: dict[str, object], *, private_ca: bool) -> None:
    if service.get("networks") != {"default": None}:
        raise DeploymentTopologyRejected("Only the provider may receive API egress")
    if service.get("command") is not None or service.get("entrypoint") is not None:
        raise DeploymentTopologyRejected("Provider image entrypoint must remain fixed")
    if service.get("stop_grace_period") != "10m0s":
        raise DeploymentTopologyRejected("Provider stop budget has drifted")
    if service.get("logging") != {
        "driver": "json-file",
        "options": {"max-file": "3", "max-size": "10m"},
    }:
        raise DeploymentTopologyRejected("Provider logging policy has drifted")
    if service.get("healthcheck") != {
        "test": ["CMD", "python", "-m", "nmrpeak_provider.provider_readiness"],
        "timeout": "2s",
        "interval": "5s",
        "retries": 3,
        "start_period": "10m0s",
    }:
        raise DeploymentTopologyRejected("Provider readiness policy has drifted")
    if service.get("tmpfs") != [
        "/tmp:size=16m,mode=1777,noexec,nosuid,nodev,uid=65532,gid=65532",
        "/run/nmrpeak-provider:size=64k,mode=0700,noexec,nosuid,nodev,uid=65532,gid=65532",
    ]:
        raise DeploymentTopologyRejected("Provider scratch posture has drifted")
    expected_mounts = {
        "/run/config/nmrpeak-provider/provider.toml": ("bind", True),
        "/run/secrets/nmrpeak-provider/signing.private.json": ("bind", True),
        "/run/secrets/nmrpeak-provider/openai-chat-completions.d": (
            "bind",
            True,
        ),
        "/run/nmrpeak-provider/frozen": ("bind", True),
        "/run/nmrpeak-provider-lock": ("volume", True),
        "/var/lib/nmrpeak-provider": ("volume", False),
        "/run/nmrpeak-provider/hf": ("volume", False),
        "/run/nmrpeak-provider/chf": ("volume", False),
    }
    if private_ca:
        expected_mounts["/run/config/nmrpeak-provider/server-a-ca.crt"] = (
            "bind",
            True,
        )
        if service.get("extra_hosts") != ["nmr.localhost=host-gateway"]:
            raise DeploymentTopologyRejected(
                "Localhost provider host-gateway mapping has drifted"
            )
    _mounts(service, expected_mounts)
    mounts = {item["target"]: item for item in service["volumes"]}
    expected_sources = {
        "/run/nmrpeak-provider-lock": "provider-identity-lock",
        "/var/lib/nmrpeak-provider": "provider-journal",
        "/run/nmrpeak-provider/hf": "hf-session",
        "/run/nmrpeak-provider/chf": "chf-session",
    }
    if any(mounts[target]["source"] != source for target, source in expected_sources.items()):
        raise DeploymentTopologyRejected("Provider received a misassigned volume")
    for target in (
        "/run/config/nmrpeak-provider/provider.toml",
        "/run/secrets/nmrpeak-provider/signing.private.json",
        "/run/secrets/nmrpeak-provider/openai-chat-completions.d",
        "/run/nmrpeak-provider/frozen",
        *(
            ("/run/config/nmrpeak-provider/server-a-ca.crt",)
            if private_ca
            else ()
        ),
    ):
        source = mounts[target]["source"]
        if type(source) is not str or not source.startswith("/"):
            raise DeploymentTopologyRejected("Provider bind source is not absolute")


def _runner_posture(
    service: dict[str, object],
    lane: str,
    checkpoint: str,
    image_input: str,
) -> None:
    if service.get("network_mode") != "none":
        raise DeploymentTopologyRejected("Runner must remain networkless")
    if service.get("command") != [
        "--checkpoint-ref",
        checkpoint,
        "--image-input-id",
        image_input,
    ]:
        raise DeploymentTopologyRejected("Runner command identity has drifted")
    if service.get("entrypoint") is not None:
        raise DeploymentTopologyRejected("Runner image entrypoint must remain fixed")
    if (
        service.get("stop_grace_period") != "20s"
        or service.get("shm_size") != "1073741824"
        or service.get("logging") != {"driver": "none"}
    ):
        raise DeploymentTopologyRejected("Runner resource or log posture has drifted")
    if service.get("tmpfs") != [
        "/tmp:size=2g,mode=1777,noexec,nosuid,nodev,uid=65532,gid=65532"
    ]:
        raise DeploymentTopologyRejected("Runner scratch posture has drifted")
    _mounts(
        service,
        {
            "/run/nmrpeak": ("volume", False),
            "/checkpoint": ("volume", True),
        },
    )
    mounts = {item["target"]: item for item in service["volumes"]}
    if (
        mounts["/run/nmrpeak"]["source"] != f"{lane}-session"
        or mounts["/checkpoint"]["source"] != f"{lane}-checkpoint"
    ):
        raise DeploymentTopologyRejected("Runner received another lane's volume")


def _mounts(service: dict[str, object], expected: dict[str, tuple[str, bool]]) -> None:
    mounts = service.get("volumes")
    if type(mounts) is not list or len(mounts) != len(expected):
        raise DeploymentTopologyRejected("Deployment mount inventory has drifted")
    observed: dict[str, tuple[str, bool]] = {}
    for mount in mounts:
        if type(mount) is not dict:
            raise DeploymentTopologyRejected("Deployment mount fields are invalid")
        fields = set(mount)
        if not {"type", "source", "target"} <= fields or fields - {
            "type", "source", "target", "read_only"
        }:
            raise DeploymentTopologyRejected("Deployment mount fields are invalid")
        target = mount.get("target")
        if type(target) is not str or target in observed:
            raise DeploymentTopologyRejected("Deployment mount target is invalid")
        observed[target] = (mount.get("type"), mount.get("read_only", False))
    if observed != expected:
        raise DeploymentTopologyRejected("Deployment mount authority has drifted")


def _volume_posture(volumes: dict[str, object]) -> None:
    for name in (
        "provider-identity-lock",
        "provider-journal",
        "hf-checkpoint",
        "chf-checkpoint",
    ):
        volume = volumes[name]
        if type(volume) is not dict or set(volume) != {"name", "external"}:
            raise DeploymentTopologyRejected("External volume fields are invalid")
        if (
            volume["external"] is not True
            or type(volume["name"]) is not str
            or not volume["name"]
        ):
            raise DeploymentTopologyRejected("External volume identity is invalid")
    expected = {
        "driver": "local",
        "driver_opts": {
            "device": "tmpfs",
            "o": "size=1m,uid=65532,gid=65532,mode=0700",
            "type": "tmpfs",
        },
    }
    for name in ("hf-session", "chf-session"):
        volume = volumes[name]
        if (
            type(volume) is not dict
            or type(volume.get("name")) is not str
            or not volume["name"]
            or {key: value for key, value in volume.items() if key != "name"}
            != expected
        ):
            raise DeploymentTopologyRejected("Session tmpfs posture has drifted")


def _project_service(
    role: str,
    service: dict[str, object],
    image_input: str,
) -> dict[str, JsonValue]:
    mounts = sorted(
        (
            {
                "target": mount["target"],
                "type": mount["type"],
                "read_only": mount.get("read_only", False),
            }
            for mount in service["volumes"]
        ),
        key=lambda mount: mount["target"],
    )
    projected: dict[str, JsonValue] = {
        "role": role,
        "image": service["image"],
        "image_input_id": image_input,
        "platform": service["platform"],
        "user": service["user"],
        "restart": service["restart"],
        "stop_grace_period": service["stop_grace_period"],
        "cpus": service["cpus"],
        "memory_bytes": int(service["mem_limit"]),
        "swap_bytes": int(service["memswap_limit"]),
        "pids": service["pids_limit"],
        "network": "provider-egress" if role == "provider" else "none",
        "logging": service["logging"],
        "read_only_root": service["read_only"],
        "cap_drop": service["cap_drop"],
        "security_opt": service["security_opt"],
        "tmpfs": service["tmpfs"],
        "mounts": mounts,
    }
    if role == "provider":
        projected["healthcheck"] = service["healthcheck"]
        if "extra_hosts" in service:
            projected["extra_hosts"] = service["extra_hosts"]
    else:
        projected["command"] = service["command"]
        projected["shared_memory_bytes"] = int(service["shm_size"])
    return projected


def _service(
    services: dict[str, object],
    name: str,
    fields: set[str],
) -> dict[str, object]:
    service = services[name]
    if type(service) is not dict or set(service) != fields:
        raise DeploymentTopologyRejected(f"Normalized {name} service fields are invalid")
    return service


def _mapping(document: dict[str, object], name: str) -> dict[str, object]:
    value = document.get(name)
    if type(value) is not dict:
        raise DeploymentTopologyRejected(f"Normalized Compose {name} must be an object")
    return value


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_REF.fullmatch(value) is not None
