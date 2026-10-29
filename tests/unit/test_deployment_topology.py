"""Prove normalized Compose grants only the fixed production authorities."""

from __future__ import annotations

import unittest
from pathlib import Path
import tempfile
from copy import deepcopy
from nmrpeak_provider.deployment_runtime import project_provider_binding
from tests.unit.test_deployment_runtime import DEFINITION

from nmrpeak_provider.canonical_json import parse_canonical_json_bytes
from repository_checks.deployment_topology import (
    DeploymentCheckpoints,
    DeploymentImages,
    DeploymentTopologyRejected,
    project_deployment_topology,
)


SHA = lambda character: "sha256:" + character * 64
IMAGES = DeploymentImages(SHA("1"), SHA("2"), SHA("3"), SHA("4"), SHA("5"), SHA("6"))
CHECKPOINTS = DeploymentCheckpoints(SHA("7"), SHA("8"))


def binding_projection(origin="https://api.example.test", *, namespace="nmrpeak-fixture"):
    with tempfile.TemporaryDirectory() as directory:
        definition = Path(directory) / "bindings.toml"
        definition.write_text(DEFINITION.replace("https://api.example.test", origin)
                              .replace("api.example.test=127.0.0.1", origin.split("://", 1)[1] + "=127.0.0.1"))
        return project_provider_binding(definition, "first", image=IMAGES.provider,
                                        namespace=namespace, input_sources={})


BINDING = binding_projection()


class DeploymentTopologyTests(unittest.TestCase):
    def test_projection_keeps_authority_but_excludes_local_object_names(self) -> None:
        topology = parse_canonical_json_bytes(
            project_deployment_topology(compose_document(), IMAGES, CHECKPOINTS, binding_projection=BINDING)
        )

        self.assertEqual(
            [item["role"] for item in topology["services"]],
            ["provider", "hf", "chf"],
        )
        self.assertEqual(
            [item["restart"] for item in topology["services"]],
            ["unless-stopped", "unless-stopped", "unless-stopped"],
        )
        self.assertEqual(topology["checkpoint_releases"], {"hf": SHA("7"), "chf": SHA("8")})
        encoded = str(topology)
        for excluded in ("deployment-name", "/host/credential", "engine-volume"):
            self.assertNotIn(excluded, encoded)

    def test_network_mount_restart_and_inventory_drift_are_rejected(self) -> None:
        mutations = []
        changed = compose_document()
        changed["services"]["hf-runner"]["network_mode"] = "default"
        mutations.append(changed)
        changed = compose_document()
        changed["services"]["chf-runner"]["volumes"][1]["read_only"] = False
        mutations.append(changed)
        changed = compose_document()
        next(m for m in changed["services"]["provider"]["volumes"] if m["target"] == "/run/nmrpeak-provider/hf")["source"] = "chf-session"
        mutations.append(changed)
        changed = compose_document()
        changed["services"]["provider"]["restart"] = "on-failure:3"
        mutations.append(changed)
        changed = compose_document()
        changed["services"]["extra"] = {}
        mutations.append(changed)
        for document in mutations:
            with self.subTest(document=document), self.assertRaises(
                DeploymentTopologyRejected
            ):
                project_deployment_topology(document, IMAGES, CHECKPOINTS, binding_projection=BINDING)

    def test_every_service_requires_retained_and_bounded_logs(self) -> None:
        for service in ("provider", "hf-runner", "chf-runner"):
            for logging in ({"driver": "none"}, {"driver": "json-file"}):
                document = compose_document()
                document["services"][service]["logging"] = logging
                with self.subTest(service=service, logging=logging):
                    with self.assertRaises(DeploymentTopologyRejected):
                        project_deployment_topology(document, IMAGES, CHECKPOINTS, binding_projection=BINDING)

    def test_provider_image_identity_changes_the_authenticated_projection(self) -> None:
        original = project_deployment_topology(
            compose_document(),
            IMAGES,
            CHECKPOINTS,
            binding_projection=BINDING)
        changed_document = compose_document()
        changed_document["services"]["provider"]["image"] = SHA("9")
        changed_images = DeploymentImages(
            SHA("9"),
            SHA("a"),
            IMAGES.hf,
            IMAGES.hf_input,
            IMAGES.chf,
            IMAGES.chf_input,
        )
        changed = project_deployment_topology(
            changed_document,
            changed_images,
            CHECKPOINTS,
            binding_projection=BINDING)

        self.assertNotEqual(changed, original)

    def test_operational_api_origin_changes_keep_the_scientific_projection(self):
        other = binding_projection("https://other.example.test")
        self.assertEqual(
            project_deployment_topology(compose_document(), IMAGES, CHECKPOINTS, binding_projection=BINDING),
            project_deployment_topology(compose_document(other), IMAGES, CHECKPOINTS, binding_projection=other),
        )

    def test_client_grants_must_match_the_actual_sdk_projection(self):
        for change in ("group", "dependency", "route", "delivery"):
            document = compose_document()
            provider = document["services"]["provider"]
            if change == "group":
                provider["group_add"] = ["1"]
            elif change == "dependency":
                provider["depends_on"] = {}
            else:
                target = (BINDING["api_route"]["client_mount"]["target"] if change == "route"
                          else str(Path(BINDING["delivery"]["socket_path"]).parent))
                next(m for m in provider["volumes"] if m["target"] == target)["read_only"] = False
            with self.subTest(change=change), self.assertRaises(DeploymentTopologyRejected):
                project_deployment_topology(document, IMAGES, CHECKPOINTS, binding_projection=BINDING)


def compose_document(binding=BINDING) -> dict[str, object]:
    common = {
        "cap_drop": ["ALL"],
        "command": None,
        "cpus": 1,
        "entrypoint": None,
        "image": IMAGES.provider,
        "init": True,
        "logging": {"driver": "json-file", "options": {"max-file": "3", "max-size": "10m"}},
        "mem_limit": "268435456",
        "memswap_limit": "268435456",
        "pids_limit": 64,
        "platform": "linux/amd64",
        "pull_policy": "never",
        "read_only": True,
        "restart": "unless-stopped",
        "security_opt": ["no-new-privileges:true"],
        "stop_grace_period": "10m0s",
        "tmpfs": [
            "/tmp:size=16m,mode=1777,noexec,nosuid,nodev,uid=65532,gid=65532",
            "/run/nmrpeak-provider:size=64k,mode=0700,noexec,nosuid,nodev,uid=65532,gid=65532",
        ],
        "user": "65532:65532",
        "volumes": [
            mount("/host/runtime", "/run/config/nmrpeak-provider", "bind", True),
            mount("/host/frozen", "/run/nmrpeak-provider/frozen", "bind", True),
            mount("provider-identity-lock", "/run/nmrpeak-provider-lock", "volume", True),
            mount("provider-journal", "/var/lib/nmrpeak-provider", "volume", False),
            mount("hf-session", "/run/nmrpeak-provider/hf", "volume", False),
            mount("chf-session", "/run/nmrpeak-provider/chf", "volume", False),
            deepcopy(binding["api_route"]["client_mount"]),
            mount("delivery-ipc", str(Path(binding["delivery"]["socket_path"]).parent), "volume", True),
        ],
        "depends_on": {name: facts | {"required": True}
                       for name, facts in binding["api_route"]["client_depends_on"].items()},
        "group_add": [str(binding["delivery"]["socket_group_id"])],
        "healthcheck": {
            "test": ["CMD", "python", "-m", "nmrpeak_provider.provider_readiness"],
            "timeout": "2s",
            "interval": "5s",
            "retries": 3,
            "start_period": "10m0s",
        },
        "networks": {"default": None},
    }
    return {
        "name": "deployment-name",
        "networks": {"default": {"name": "deployment-name_default", "ipam": {}},
                     **deepcopy(binding["api_route"]["compose"]["networks"])},
        "services": {
            **deepcopy(binding["api_route"]["compose"]["services"]),
            "provider": common,
            "hf-runner": runner("hf", IMAGES.hf, IMAGES.hf_input, CHECKPOINTS.hf),
            "chf-runner": runner("chf", IMAGES.chf, IMAGES.chf_input, CHECKPOINTS.chf),
        },
        "volumes": {
            **deepcopy(binding["api_route"]["compose"]["volumes"]),
            "delivery-ipc": {"external": True, "name": binding["delivery"]["ipc_volume"]},
            "provider-identity-lock": external_volume("engine-volume-lock"),
            "provider-journal": external_volume("engine-volume-journal"),
            "hf-checkpoint": external_volume("engine-volume-hf"),
            "chf-checkpoint": external_volume("engine-volume-chf"),
            "hf-session": session_volume("deployment-name_hf-session"),
            "chf-session": session_volume("deployment-name_chf-session"),
        },
    }


def runner(lane: str, image: str, image_input: str, checkpoint: str) -> dict[str, object]:
    return {
        "cap_drop": ["ALL"],
        "command": ["--checkpoint-ref", checkpoint, "--image-input-id", image_input],
        "cpus": 8,
        "entrypoint": None,
        "image": image,
        "init": True,
        "logging": {"driver": "json-file", "options": {"max-file": "3", "max-size": "10m"}},
        "mem_limit": "34359738368",
        "memswap_limit": "34359738368",
        "network_mode": "none",
        "pids_limit": 256,
        "platform": "linux/amd64",
        "pull_policy": "never",
        "read_only": True,
        "restart": "unless-stopped",
        "security_opt": ["no-new-privileges:true"],
        "shm_size": "1073741824",
        "stop_grace_period": "20s",
        "tmpfs": ["/tmp:size=2g,mode=1777,noexec,nosuid,nodev,uid=65532,gid=65532"],
        "user": "65532:65532",
        "volumes": [
            mount(f"{lane}-session", "/run/nmrpeak", "volume", False),
            mount(f"{lane}-checkpoint", "/checkpoint", "volume", True),
        ],
    }


def mount(source: str, target: str, kind: str, read_only: bool) -> dict[str, object]:
    value: dict[str, object] = {"source": source, "target": target, "type": kind}
    if read_only:
        value["read_only"] = True
    return value


def external_volume(name: str) -> dict[str, object]:
    return {"name": name, "external": True}


def session_volume(name: str) -> dict[str, object]:
    return {
        "name": name,
        "driver": "local",
        "driver_opts": {
            "device": "tmpfs",
            "o": "size=1m,uid=65532,gid=65532,mode=0700",
            "type": "tmpfs",
        },
    }


if __name__ == "__main__":
    unittest.main()
