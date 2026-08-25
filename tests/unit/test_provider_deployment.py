"""Prove initialization publishes only a new literal deployment config."""

from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import zipfile

import deployment.provider_deployment as provider_deployment
from deployment.local_image import LocalImage
from deployment.provider_deployment import (
    DeploymentOperationRejected,
    deployment_plan_bytes,
    initialize_deployment,
    render_deployment_plan,
)
from nmrpeak_provider.canonical_json import parse_canonical_json_bytes
from repository_checks.chf_release import ARCHIVE_MEMBER as CHF_MEMBER
from repository_checks.chf_release import candidate_release_bytes as chf_release_bytes
from repository_checks.hf_release import ARCHIVE_MEMBER as HF_MEMBER
from repository_checks.hf_release import candidate_release_bytes as hf_release_bytes
from tests.unit.test_deployment_topology import compose_document


ROOT = Path(__file__).parents[2]
SOURCE_REVISION = "1" * 40
INPUTS = {
    "provider": "sha256:" + "2" * 64,
    "hf": "sha256:" + "4" * 64,
    "chf": "sha256:" + "6" * 64,
}
IMAGES = {
    "provider": LocalImage("sha256:" + "1" * 64, INPUTS["provider"]),
    "hf": LocalImage("sha256:" + "3" * 64, INPUTS["hf"]),
    "chf": LocalImage("sha256:" + "5" * 64, INPUTS["chf"]),
}


class ProviderDeploymentTests(unittest.TestCase):
    def test_initialization_copies_committed_examples_once(self) -> None:
        with committed_repository() as repository:
            destination = initialize_deployment(repository, "production-1")

            self.assertEqual(
                (destination / "provider.toml").read_bytes(),
                b"provider example\n",
            )
            self.assertEqual(
                (destination / "deployment.toml").read_bytes(),
                b"deployment example\n",
            )
            with self.assertRaisesRegex(
                DeploymentOperationRejected,
                "already exists",
            ):
                initialize_deployment(repository, "production-1")

    def test_invalid_name_and_dirty_checkout_publish_nothing(self) -> None:
        with committed_repository() as repository:
            for name in ("../escape", "UPPER", "-leading"):
                with self.subTest(name=name), self.assertRaises(
                    DeploymentOperationRejected
                ):
                    initialize_deployment(repository, name)
            (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(
                DeploymentOperationRejected,
                "clean committed checkout",
            ):
                initialize_deployment(repository, "dirty")
            self.assertFalse((repository / "config/deployments").exists())

    def test_concurrent_initializers_cannot_replace_the_winner(self) -> None:
        with committed_repository() as repository:
            barrier = threading.Barrier(2)
            original = provider_deployment._require_clean_checkout
            outcomes: list[Path | BaseException] = []

            def synchronized_preflight(path: Path) -> None:
                original(path)
                barrier.wait(timeout=2)

            def initialize() -> None:
                try:
                    outcomes.append(initialize_deployment(repository, "production"))
                except BaseException as error:
                    outcomes.append(error)

            with patch.object(
                provider_deployment,
                "_require_clean_checkout",
                side_effect=synchronized_preflight,
            ):
                threads = (
                    threading.Thread(target=initialize),
                    threading.Thread(target=initialize),
                )
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)

            self.assertEqual(sum(isinstance(item, Path) for item in outcomes), 1)
            failures = [item for item in outcomes if isinstance(item, BaseException)]
            self.assertEqual(len(failures), 1)
            self.assertIn("already exists", str(failures[0]))

    def test_config_plan_joins_artifacts_without_publishing_state(self) -> None:
        with render_repository() as repository:
            renders: list[dict[str, str]] = []

            def render_compose(
                _repository: Path,
                deployment: str,
                environment: dict[str, str],
                _docker: Path,
            ) -> dict[str, object]:
                renders.append(environment)
                document = compose_document()
                document["name"] = f"nmrpeak-{deployment}"
                services = document["services"]
                services["provider"]["image"] = environment["PROVIDER_IMAGE_REF"]
                services["hf-runner"]["image"] = environment["HF_RUNNER_IMAGE_REF"]
                services["chf-runner"]["image"] = environment["CHF_RUNNER_IMAGE_REF"]
                services["hf-runner"]["command"] = [
                    "--checkpoint-ref",
                    environment["HF_CHECKPOINT_REF"],
                    "--image-input-id",
                    environment["HF_RUNNER_IMAGE_INPUT_ID"],
                ]
                services["chf-runner"]["command"] = [
                    "--checkpoint-ref",
                    environment["CHF_CHECKPOINT_REF"],
                    "--image-input-id",
                    environment["CHF_RUNNER_IMAGE_INPUT_ID"],
                ]
                provider_mounts = services["provider"]["volumes"]
                provider_mounts[0]["source"] = environment["PROVIDER_CONFIG_PATH"]
                provider_mounts[1]["source"] = environment["PROVIDER_CREDENTIAL_PATH"]
                provider_mounts[2]["source"] = environment["FROZEN_GENERATION_PATH"]
                return document

            with (
                patch.object(
                    provider_deployment,
                    "_image_input_ids",
                    return_value=INPUTS,
                ),
                patch.object(
                    provider_deployment,
                    "_local_images",
                    return_value=IMAGES,
                ),
                patch.object(
                    provider_deployment,
                    "_render_compose",
                    side_effect=render_compose,
                ),
            ):
                plan = render_deployment_plan(repository, "production")
                self.assertEqual(len(renders), 2)
                provider_config = repository / "config/deployments/production/provider.toml"
                provider_config.write_bytes(
                    provider_config.read_bytes().replace(
                        b"feed_interval_seconds = 5",
                        b"feed_interval_seconds = 6",
                    )
                )
                with patch.object(
                    provider_deployment,
                    "_require_clean_checkout",
                ):
                    changed_config_plan = render_deployment_plan(
                        repository,
                        "production",
                    )
                self.assertEqual(
                    changed_config_plan.generation.frozen_generation_id,
                    plan.generation.frozen_generation_id,
                )
                self.assertNotEqual(
                    changed_config_plan.runtime_config_id,
                    plan.runtime_config_id,
                )
                self.assertNotEqual(
                    renders[1]["PROVIDER_CONFIG_PATH"],
                    renders[3]["PROVIDER_CONFIG_PATH"],
                )
                self.assertEqual(
                    renders[1]["FROZEN_GENERATION_PATH"],
                    renders[3]["FROZEN_GENERATION_PATH"],
                )
                provider_config.write_bytes(
                    provider_config.read_bytes().replace(
                        b"[server_a]\n",
                        b"[server_a]\nuse_private_ca = true\n",
                    )
                )
                with (
                    patch.object(
                        provider_deployment,
                        "_require_clean_checkout",
                    ),
                    self.assertRaisesRegex(
                        DeploymentOperationRejected,
                        "cannot select a private Server A CA",
                    ),
                ):
                    render_deployment_plan(repository, "production")

            preview = parse_canonical_json_bytes(deployment_plan_bytes(plan))
            self.assertEqual(preview["kind"], "read_only_preview")
            self.assertEqual(
                preview["frozen_generation_id"],
                plan.generation.frozen_generation_id,
            )
            self.assertEqual(preview["runtime_config_id"], plan.runtime_config_id)
            self.assertEqual(
                set(preview["artifacts"]),
                {"provider.toml", "frozen/manifest.json", "frozen/files"},
            )
            self.assertIn(
                plan.generation.frozen_generation_id,
                renders[1]["FROZEN_GENERATION_PATH"],
            )
            self.assertIn(
                plan.runtime_config_id,
                renders[1]["PROVIDER_CONFIG_PATH"],
            )
            self.assertFalse((repository / "secrets").exists())


class committed_repository:
    def __init__(self) -> None:
        self.temporary = TemporaryDirectory()

    def __enter__(self) -> Path:
        root = Path(self.temporary.name).resolve()
        (root / "config").mkdir()
        (root / "config/provider.toml.example").write_bytes(b"provider example\n")
        (root / "config/deployment.toml.example").write_bytes(
            b"deployment example\n"
        )
        (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
        (root / ".gitignore").write_text(
            "/config/deployments/\n",
            encoding="utf-8",
        )
        subprocess.run(("git", "init", "-q", str(root)), check=True)
        subprocess.run(("git", "-C", str(root), "add", "."), check=True)
        subprocess.run(
            (
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "commit",
                "-qm",
                "Add templates",
            ),
            check=True,
        )
        return root

    def __exit__(self, *exc_info: object) -> None:
        self.temporary.cleanup()


class render_repository:
    def __init__(self) -> None:
        self.temporary = TemporaryDirectory()

    def __enter__(self) -> Path:
        root = Path(self.temporary.name).resolve()
        paths = (
            "config/deployments/production",
            "families/nmrpeak",
            "models/nmrpeak_hf_v1/releases",
            "models/nmrpeak_hf_v1/provider",
            "models/nmrpeak_chf_v1/releases",
            "models/nmrpeak_chf_v1/provider",
            "compose",
        )
        for relative in paths:
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "config/deployments/production/provider.toml").write_bytes(
            (ROOT / "config/provider.toml.example").read_bytes()
        )
        (root / "config/deployments/production/deployment.toml").write_text(
            selection(),
            encoding="utf-8",
        )
        (root / "families/nmrpeak/source-closure.paths").write_text(
            f"source_revision {SOURCE_REVISION}\nLICENSE\n",
            encoding="ascii",
        )
        (root / "models/nmrpeak_hf_v1/provider/hello.txt").write_text(
            "HF description\n",
            encoding="utf-8",
        )
        (root / "models/nmrpeak_chf_v1/provider/hello.txt").write_text(
            "CHF description\n",
            encoding="utf-8",
        )
        archive = root / "weights.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(HF_MEMBER, b"hf checkpoint")
            bundle.writestr(CHF_MEMBER, b"chf checkpoint")
        (root / "models/nmrpeak_hf_v1/releases/hf-release.json").write_bytes(
            hf_release_bytes(
                archive,
                "hf-release",
                source_revision=SOURCE_REVISION,
            )
        )
        (root / "models/nmrpeak_chf_v1/releases/chf-release.json").write_bytes(
            chf_release_bytes(
                archive,
                "chf-release",
                source_revision=SOURCE_REVISION,
            )
        )
        (root / "compose/provider.yml").write_text("test fixture\n", encoding="utf-8")
        archive.unlink()
        subprocess.run(("git", "init", "-q", str(root)), check=True)
        subprocess.run(("git", "-C", str(root), "add", "."), check=True)
        subprocess.run(
            (
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "commit",
                "-qm",
                "Add deployment fixtures",
            ),
            check=True,
        )
        return root

    def __exit__(self, *exc_info: object) -> None:
        self.temporary.cleanup()


def selection() -> str:
    return '''schema_id = "nmrpeak.named_deployment.v1"
provider_ref = "provider:nmrpeak"

[implementations.hf]
target = "cpu-x86_64"
release = "hf-release"

[implementations.hf.run_generation]
generation_id = "hf-generation"
not_before = "2026-08-25T00:00:00Z"

[implementations.chf]
target = "cpu-x86_64"
release = "chf-release"

[implementations.chf.run_generation]
generation_id = "chf-generation"
not_before = "2026-08-25T00:00:00Z"
'''


if __name__ == "__main__":
    unittest.main()
