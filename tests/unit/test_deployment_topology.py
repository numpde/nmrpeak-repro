"""Prove normalized Compose grants only the fixed production authorities."""

from __future__ import annotations

import unittest

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


class DeploymentTopologyTests(unittest.TestCase):
    def test_projection_keeps_authority_but_excludes_local_object_names(self) -> None:
        topology = parse_canonical_json_bytes(
            project_deployment_topology(compose_document(), IMAGES, CHECKPOINTS)
        )

        self.assertEqual(
            [item["role"] for item in topology["services"]],
            ["provider", "hf", "chf"],
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
        changed["services"]["provider"]["volumes"][5]["source"] = "chf-session"
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
                project_deployment_topology(document, IMAGES, CHECKPOINTS)

    def test_provider_image_identity_changes_the_authenticated_projection(self) -> None:
        original = project_deployment_topology(
            compose_document(),
            IMAGES,
            CHECKPOINTS,
        )
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
        )

        self.assertNotEqual(changed, original)

    def test_private_ca_topology_admits_only_the_fixed_provider_overlay(self) -> None:
        document = compose_document()
        provider = document["services"]["provider"]
        provider["extra_hosts"] = ["nmr.localhost=host-gateway"]
        provider["volumes"].append(
            mount(
                "/host/ca.crt",
                "/run/config/nmrpeak-provider/server-a-ca.crt",
                "bind",
                True,
            )
        )

        topology = parse_canonical_json_bytes(
            project_deployment_topology(
                document,
                IMAGES,
                CHECKPOINTS,
                private_ca=True,
            )
        )

        self.assertEqual(
            topology["services"][0]["extra_hosts"],
            ["nmr.localhost=host-gateway"],
        )
        self.assertNotIn("/host/ca.crt", str(topology))
        with self.assertRaises(DeploymentTopologyRejected):
            project_deployment_topology(document, IMAGES, CHECKPOINTS)


def compose_document() -> dict[str, object]:
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
        "restart": "no",
        "security_opt": ["no-new-privileges:true"],
        "stop_grace_period": "10m0s",
        "tmpfs": [
            "/tmp:size=16m,mode=1777,noexec,nosuid,nodev,uid=65532,gid=65532",
            "/run/nmrpeak-provider:size=64k,mode=0700,noexec,nosuid,nodev,uid=65532,gid=65532",
        ],
        "user": "65532:65532",
        "volumes": [
            mount("/host/provider", "/run/config/nmrpeak-provider/provider.toml", "bind", True),
            mount(
                "/host/credential",
                "/run/secrets/nmrpeak-provider/signing.private.json",
                "bind",
                True,
            ),
            mount("/host/frozen", "/run/nmrpeak-provider/frozen", "bind", True),
            mount("provider-identity-lock", "/run/nmrpeak-provider-lock", "volume", True),
            mount("provider-journal", "/var/lib/nmrpeak-provider", "volume", False),
            mount("hf-session", "/run/nmrpeak-provider/hf", "volume", False),
            mount("chf-session", "/run/nmrpeak-provider/chf", "volume", False),
            mount(
                "/host/interpreter",
                "/run/secrets/nmrpeak-provider/openai-chat-completions.d",
                "bind",
                True,
            ),
        ],
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
        "networks": {"default": {"name": "deployment-name_default", "ipam": {}}},
        "services": {
            "provider": common,
            "hf-runner": runner("hf", IMAGES.hf, IMAGES.hf_input, CHECKPOINTS.hf),
            "chf-runner": runner("chf", IMAGES.chf, IMAGES.chf_input, CHECKPOINTS.chf),
        },
        "volumes": {
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
        "logging": {"driver": "none"},
        "mem_limit": "34359738368",
        "memswap_limit": "34359738368",
        "network_mode": "none",
        "pids_limit": 256,
        "platform": "linux/amd64",
        "pull_policy": "never",
        "read_only": True,
        "restart": "no",
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
