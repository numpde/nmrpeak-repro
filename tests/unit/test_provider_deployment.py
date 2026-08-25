"""Prove initialization publishes only a new literal deployment config."""

from __future__ import annotations

from contextlib import nullcontext
from io import BytesIO
import json
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import deployment.provider_deployment as provider_deployment
from deployment.local_image import LocalImage
from deployment.provider_deployment import (
    DeploymentPlan,
    DeploymentOperationRejected,
    deployment_status_bytes,
    deployment_plan_bytes,
    initialize_deployment,
    install_provider_credential,
    materialize_deployment_plan,
    remove_frozen_generation,
    retire_provider_journal,
    render_deployment_plan,
    show_provider_logs,
    start_deployment,
    stop_deployment,
)
from nmrpeak_provider.attempt_inventory import (
    AttemptInventory,
    AttemptInventoryReadFailed,
)
from nmrpeak_provider.canonical_json import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from repository_checks.chf_release import ARCHIVE_MEMBER as CHF_MEMBER
from repository_checks.chf_release import candidate_release_bytes as chf_release_bytes
from repository_checks.hf_release import ARCHIVE_MEMBER as HF_MEMBER
from repository_checks.hf_release import candidate_release_bytes as hf_release_bytes
from tests.unit.test_deployment_topology import compose_document
from tests.unit.test_provider_startup_inputs import (
    credential_bytes,
    credential_document,
)


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
                *,
                localhost: bool,
            ) -> dict[str, object]:
                renders.append(environment)
                return compose_for_environment(
                    deployment,
                    environment,
                    localhost=localhost,
                )

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
            self.assertEqual(
                Path(renders[1]["FROZEN_GENERATION_PATH"]).parent.name,
                plan.generation.frozen_generation_id.removeprefix("sha256:"),
            )
            self.assertEqual(
                Path(renders[1]["PROVIDER_CONFIG_PATH"]).parent.name,
                plan.runtime_config_id.removeprefix("sha256:"),
            )
            self.assertFalse((repository / "secrets").exists())

    def test_materialization_is_exact_idempotent_and_rejects_drift(self) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)

            first = materialize_deployment_plan(repository, "production", plan)
            second = materialize_deployment_plan(repository, "production", plan)
            self.assertEqual(first, second)
            self.assertEqual(
                first[0].parent.name,
                plan.runtime_config_id.removeprefix("sha256:"),
            )
            self.assertEqual(
                first[1].parent.name,
                plan.generation.frozen_generation_id.removeprefix("sha256:"),
            )
            self.assertEqual(first[0].read_bytes(), plan.generation.provider_config)
            self.assertEqual(
                (first[1] / "manifest.json").read_bytes(),
                plan.generation.manifest,
            )
            self.assertEqual(
                (first[1] / "hello/hf.txt").read_bytes(),
                b"HF description\n",
            )
            self.assertEqual(
                stat.S_IMODE(first[0].stat().st_mode),
                0o440,
            )
            self.assertEqual(
                provider_deployment._read_acl(
                    first[0],
                    "Retained deployment file",
                ),
                provider_deployment._PROVIDER_READONLY_FILE_ACL,
            )
            self.assertEqual(
                stat.S_IMODE(
                    (repository / "secrets/deployments/production/.lifecycle.lock")
                    .stat()
                    .st_mode
                ),
                0o600,
            )

            first[0].chmod(0o600)
            first[0].write_bytes(b"drift")
            provider_deployment._grant_provider_file_access(
                first[0],
                owner_write=False,
            )
            with self.assertRaisesRegex(
                DeploymentOperationRejected,
                "bytes have drifted",
            ):
                materialize_deployment_plan(repository, "production", plan)

    def test_localhost_plan_grants_only_provider_host_gateway_and_ca(self) -> None:
        with render_repository() as repository, TemporaryDirectory() as temporary:
            provider_config = repository / "config/deployments/production/provider.toml"
            provider_config.write_bytes(
                provider_config.read_bytes()
                .replace(b"https://api.example.test", b"https://nmr.localhost:10443")
                .replace(b'topology = "web"', b'topology = "dev-local"')
                .replace(
                    b"[server_a]\n",
                    b"[server_a]\nuse_private_ca = true\n",
                )
            )
            certificate = Path(temporary).resolve() / "ca.crt"
            certificate.write_text("test CA\n", encoding="ascii")

            with (
                patch.object(provider_deployment, "_require_clean_checkout"),
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
                    side_effect=lambda _, deployment, environment, __, *, localhost: (
                        compose_for_environment(
                            deployment,
                            environment,
                            localhost=localhost,
                        )
                    ),
                ),
            ):
                plan = render_deployment_plan(
                    repository,
                    "production",
                    localhost_ca_certificate=certificate,
                )

            provider = plan.compose["services"]["provider"]
            self.assertEqual(
                provider["extra_hosts"],
                ["nmr.localhost=host-gateway"],
            )
            self.assertIn(
                {
                    "type": "bind",
                    "source": str(certificate),
                    "target": "/run/config/nmrpeak-provider/server-a-ca.crt",
                    "read_only": True,
                },
                provider["volumes"],
            )
            for runner in ("hf-runner", "chf-runner"):
                service = plan.compose["services"][runner]
                self.assertEqual(service["network_mode"], "none")
                self.assertNotIn("extra_hosts", service)
                self.assertNotIn("server-a-ca.crt", str(service["volumes"]))

    def test_localhost_ca_path_is_explicit_resolved_and_mode_matched(self) -> None:
        with render_repository() as repository, TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            certificate = directory / "ca.crt"
            certificate.write_text("test CA\n", encoding="ascii")
            symlink = directory / "ca-link.crt"
            symlink.symlink_to(certificate)

            for invalid, diagnostic in (
                (Path("relative-ca.crt"), "absolute path"),
                (symlink, "resolved non-symlink"),
            ):
                with self.subTest(path=invalid), self.assertRaisesRegex(
                    DeploymentOperationRejected,
                    diagnostic,
                ):
                    render_deployment_plan(
                        repository,
                        "production",
                        localhost_ca_certificate=invalid,
                    )

            with self.assertRaisesRegex(
                DeploymentOperationRejected,
                "requires the dev-local nmr.localhost origin",
            ):
                render_deployment_plan(
                    repository,
                    "production",
                    localhost_ca_certificate=certificate,
                )

    def test_start_admits_every_input_before_compose_and_readiness_proof(self) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)

            events: list[str] = []
            with (
                patch.object(
                    provider_deployment,
                    "render_deployment_plan",
                    return_value=plan,
                ),
                patch.object(
                    provider_deployment,
                    "_admit_installed_credential",
                    side_effect=lambda *_: events.append("credential"),
                ),
                patch.object(
                    provider_deployment,
                    "_admit_interpreter_configs",
                    side_effect=lambda *_: events.append("interpreter"),
                ),
                patch.object(
                    provider_deployment,
                    "verify_hf_checkpoint",
                    side_effect=lambda *_args, **_kwargs: events.append("hf"),
                ),
                patch.object(
                    provider_deployment,
                    "verify_chf_checkpoint",
                    side_effect=lambda *_args, **_kwargs: events.append("chf"),
                ),
                patch.object(
                    provider_deployment,
                    "ensure_provider_state_volumes",
                    side_effect=lambda *_: events.append("provider_volumes"),
                ),
                patch.object(
                    provider_deployment,
                    "_inspect_project_containers",
                    side_effect=({}, ready_services(plan)),
                ),
                patch.object(
                    provider_deployment,
                    "_run_compose_plan",
                    side_effect=lambda *_: events.append("compose_up"),
                ),
            ):
                self.assertEqual(
                    start_deployment(repository, "production"),
                    plan,
                )

            self.assertEqual(
                events,
                [
                    "credential",
                    "interpreter",
                    "hf",
                    "chf",
                    "provider_volumes",
                    "compose_up",
                ],
            )
            self.assertTrue(
                (
                    repository
                    / "secrets/deployments/production"
                    / "generations"
                    / plan.generation.frozen_generation_id.removeprefix("sha256:")
                    / "frozen/manifest.json"
                ).is_file()
            )

    def test_start_stops_before_engine_effects_when_input_access_fails(self) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)

            with (
                patch.object(
                    provider_deployment,
                    "render_deployment_plan",
                    return_value=plan,
                ),
                patch.object(
                    provider_deployment,
                    "_grant_provider_tree_access",
                    side_effect=DeploymentOperationRejected(
                        "Provider input access operation was rejected"
                    ),
                ),
                patch.object(
                    provider_deployment,
                    "verify_hf_checkpoint",
                ) as verify_hf,
                patch.object(
                    provider_deployment,
                    "verify_chf_checkpoint",
                ) as verify_chf,
                patch.object(
                    provider_deployment,
                    "ensure_provider_state_volumes",
                ) as ensure_volumes,
                patch.object(
                    provider_deployment,
                    "_run_compose_plan",
                ) as compose_up,
                self.assertRaisesRegex(
                    DeploymentOperationRejected,
                    "input access operation was rejected",
                ),
            ):
                start_deployment(repository, "production")

            verify_hf.assert_not_called()
            verify_chf.assert_not_called()
            ensure_volumes.assert_not_called()
            compose_up.assert_not_called()

    def test_startup_credential_must_match_provider_and_private_posture(self) -> None:
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            credential = state / "signing.private.json"
            credential.write_bytes(credential_bytes(Ed25519PrivateKey.generate()))
            credential.chmod(0o600)
            provider_deployment._admit_installed_credential(
                state,
                "provider:nmrpeak",
            )
            with self.assertRaisesRegex(
                DeploymentOperationRejected,
                "another provider",
            ):
                provider_deployment._admit_installed_credential(
                    state,
                    "provider:other",
                )
            credential.chmod(0o644)
            with self.assertRaisesRegex(
                DeploymentOperationRejected,
                "exact private access",
            ):
                provider_deployment._admit_installed_credential(
                    state,
                    "provider:nmrpeak",
                )

    def test_interpreter_config_grants_only_one_bounded_toml_tree(self) -> None:
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            directory = state / "openai-chat-completions.d"
            directory.mkdir(mode=0o700)
            endpoint = directory / "10-primary.toml"
            endpoint.write_text(
                'id="primary"\nbase_url="https://example.test"\n'
                'api_key="secret"\nmodel="model"\n',
                encoding="utf-8",
            )
            endpoint.chmod(0o600)
            with (
                patch.object(
                    provider_deployment,
                    "_read_acl",
                    return_value=provider_deployment._PRIVATE_WRITABLE_FILE_ACL,
                ),
                patch.object(
                    provider_deployment,
                    "_grant_provider_tree_access",
                ) as grant,
            ):
                provider_deployment._admit_interpreter_configs(state)
            grant.assert_called_once_with(directory)

            with (
                patch.object(
                    provider_deployment,
                    "_read_acl",
                    return_value=provider_deployment._PROVIDER_READONLY_FILE_ACL,
                ),
                patch.object(
                    provider_deployment,
                    "_grant_provider_tree_access",
                ) as grant,
            ):
                provider_deployment._admit_interpreter_configs(state)
            grant.assert_called_once_with(directory)

            (directory / "README").write_text("unexpected", encoding="utf-8")
            with (
                patch.object(
                    provider_deployment,
                    "_read_acl",
                    return_value=provider_deployment._PRIVATE_WRITABLE_FILE_ACL,
                ),
                self.assertRaisesRegex(
                    DeploymentOperationRejected,
                    "invalid file",
                ),
            ):
                provider_deployment._admit_interpreter_configs(state)

    def test_credential_install_is_idempotent_and_replaces_only_while_stopped(
        self,
    ) -> None:
        with render_repository() as repository, TemporaryDirectory() as temporary:
            api_root = Path(temporary).resolve()
            source = write_api_credential(
                api_root,
                credential_bytes(Ed25519PrivateKey.generate()),
            )
            destination = install_provider_credential(
                repository,
                "production",
                api_root,
            )
            original_inode = destination.stat().st_ino
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o640)
            self.assertEqual(
                provider_deployment._read_acl(
                    destination,
                    "Provider signing credential",
                ),
                provider_deployment._PROVIDER_WRITABLE_FILE_ACL,
            )
            self.assertEqual(
                install_provider_credential(repository, "production", api_root),
                destination,
            )
            self.assertEqual(destination.stat().st_ino, original_inode)

            replacement = credential_bytes(Ed25519PrivateKey.generate())
            source.write_bytes(replacement)
            source.chmod(0o600)
            with self.assertRaisesRegex(
                DeploymentOperationRejected,
                "REPLACE=1",
            ):
                install_provider_credential(repository, "production", api_root)
            self.assertNotEqual(destination.read_bytes(), replacement)

            with (
                patch.object(
                    provider_deployment,
                    "_inspect_project_containers",
                    return_value={
                        "provider": {"State": {"Running": True}},
                    },
                ),
                self.assertRaisesRegex(
                    DeploymentOperationRejected,
                    "proved-stopped",
                ),
            ):
                install_provider_credential(
                    repository,
                    "production",
                    api_root,
                    replace=True,
                )
            self.assertNotEqual(destination.read_bytes(), replacement)

            with patch.object(
                provider_deployment,
                "_inspect_project_containers",
                return_value={
                    "provider": {"State": {"Running": False}},
                },
            ):
                install_provider_credential(
                    repository,
                    "production",
                    api_root,
                    replace=True,
                )
            self.assertEqual(destination.read_bytes(), replacement)

    def test_credential_install_rejects_ambiguous_or_symlinked_api_artifacts(
        self,
    ) -> None:
        with render_repository() as repository, TemporaryDirectory() as temporary:
            api_root = Path(temporary).resolve()
            run_document = credential_document(Ed25519PrivateKey.generate())
            write_api_credential(
                api_root,
                canonical_json_bytes(run_document) + b"\n",
            )
            dev_document = {**run_document, "profile": "dev"}
            write_api_credential(
                api_root,
                canonical_json_bytes(dev_document) + b"\n",
                profile="dev",
            )
            with self.assertRaisesRegex(
                DeploymentOperationRejected,
                "exactly one matching",
            ):
                install_provider_credential(repository, "production", api_root)

        with render_repository() as repository, TemporaryDirectory() as temporary:
            api_root = Path(temporary).resolve()
            outside = api_root / "outside.json"
            outside.write_bytes(credential_bytes(Ed25519PrivateKey.generate()))
            outside.chmod(0o600)
            candidate = api_root / "secrets/run/providers/nmrpeak/signing.private.json"
            candidate.parent.mkdir(parents=True)
            candidate.symlink_to(outside)
            with self.assertRaisesRegex(
                DeploymentOperationRejected,
                "path is invalid",
            ):
                install_provider_credential(repository, "production", api_root)

    def test_compose_up_consumes_the_exact_normalized_plan(self) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)
            captured: dict[str, object] = {}

            def docker_command(
                _docker: Path,
                arguments: tuple[str, ...],
                *,
                timeout: int,
            ):
                compose_path = Path(arguments[arguments.index("--file") + 1])
                captured["compose"] = parse_canonical_json_bytes(
                    compose_path.read_bytes()
                )
                captured["arguments"] = arguments
                captured["timeout"] = timeout
                return subprocess.CompletedProcess((), 0, b"", b"")

            with patch.object(
                provider_deployment,
                "_docker_command",
                side_effect=docker_command,
            ):
                provider_deployment._run_compose_plan(
                    Path("/usr/bin/docker"),
                    repository,
                    "production",
                    plan,
                )

        self.assertEqual(captured["compose"], plan.compose)
        arguments = captured["arguments"]
        self.assertIn("--no-build", arguments)
        self.assertEqual(
            arguments[arguments.index("--pull") : arguments.index("--pull") + 2],
            ("--pull", "never"),
        )
        self.assertEqual(captured["timeout"], 720)

    def test_localhost_render_selects_the_exact_overlay(self) -> None:
        rendered = canonical_json_bytes(compose_document())
        invocations: list[tuple[str, ...]] = []

        def run(arguments: tuple[str, ...], **_kwargs: object):
            invocations.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, rendered, b"")

        with patch.object(subprocess, "run", side_effect=run):
            provider_deployment._render_compose(
                ROOT,
                "localhost",
                {"PATH": "/usr/bin:/bin"},
                Path("/usr/bin/docker"),
                localhost=True,
            )
            provider_deployment._render_compose(
                ROOT,
                "production",
                {"PATH": "/usr/bin:/bin"},
                Path("/usr/bin/docker"),
                localhost=False,
            )

        localhost_files = [
            invocations[0][index + 1]
            for index, value in enumerate(invocations[0])
            if value == "--file"
        ]
        public_files = [
            invocations[1][index + 1]
            for index, value in enumerate(invocations[1])
            if value == "--file"
        ]
        self.assertEqual(
            localhost_files,
            [
                str(ROOT / "compose/provider.yml"),
                str(ROOT / "compose/provider-localhost.yml"),
            ],
        )
        self.assertEqual(public_files, [str(ROOT / "compose/provider.yml")])

    def test_project_inspection_rejects_a_foreign_checkout(self) -> None:
        container_id = "a" * 64
        inspection = [
            {
                "Id": container_id,
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "nmrpeak-production",
                        "com.docker.compose.service": "provider",
                        "com.docker.compose.oneoff": "False",
                        "com.docker.compose.project.working_dir": "/foreign",
                    }
                },
            }
        ]
        outputs = (
            subprocess.CompletedProcess((), 0, (container_id + "\n").encode(), b""),
            subprocess.CompletedProcess((), 0, json.dumps(inspection).encode(), b""),
        )
        with patch.object(
            provider_deployment,
            "_docker_command",
            side_effect=outputs,
        ), self.assertRaisesRegex(
            DeploymentOperationRejected,
            "foreign or ambiguous",
        ):
            provider_deployment._inspect_project_containers(
                Path("/usr/bin/docker"),
                Path("/repository"),
                "production",
            )

    def test_status_reports_stopped_owned_services_without_config(self) -> None:
        provider = {
            "Id": "a" * 64,
            "Image": "sha256:" + "b" * 64,
            "State": {"Status": "exited", "ExitCode": 2},
        }
        with patch.object(
            provider_deployment,
            "_inspect_project_containers",
            return_value={"provider": provider},
        ):
            status_document = parse_canonical_json_bytes(
                deployment_status_bytes(ROOT, "production")
            )

        self.assertEqual(
            status_document["services"],
            [
                {
                    "service": "provider",
                    "container_id": "a" * 64,
                    "image_id": "sha256:" + "b" * 64,
                    "state": "exited",
                    "health": None,
                }
            ],
        )

    def test_logs_follow_only_a_running_owned_provider(self) -> None:
        provider = {
            "Id": "a" * 64,
            "State": {"Running": True},
        }
        with (
            patch.object(
                provider_deployment,
                "_inspect_project_containers",
                return_value={"provider": provider},
            ),
            patch.object(provider_deployment.os, "execve") as execute,
        ):
            show_provider_logs(ROOT, "production")

        executable, arguments, environment = execute.call_args.args
        self.assertEqual(executable, "/usr/bin/docker")
        self.assertEqual(
            arguments,
            [
                "/usr/bin/docker",
                "--context",
                "default",
                "logs",
                "--timestamps",
                "--tail",
                "200",
                "--follow",
                "a" * 64,
            ],
        )
        self.assertEqual(
            environment,
            {
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                "DOCKER_CONTEXT": "default",
            },
        )

        provider["State"] = {"Running": False}
        with (
            patch.object(
                provider_deployment,
                "_inspect_project_containers",
                return_value={"provider": provider},
            ),
            patch.object(provider_deployment.os, "execve") as execute,
        ):
            show_provider_logs(ROOT, "production")
        self.assertNotIn("--follow", execute.call_args.args[1])

    def test_down_refuses_foreign_resources_before_compose(self) -> None:
        with TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            state = repository / "secrets/deployments/production"
            state.mkdir(parents=True, mode=0o700)
            state.chmod(0o700)
            with (
                patch.object(
                    provider_deployment,
                    "_inspect_project_containers",
                    return_value={},
                ),
                patch.object(
                    provider_deployment,
                    "_docker_command",
                    return_value=subprocess.CompletedProcess(
                        (),
                        0,
                        b"nmrpeak-production_foreign\n",
                        b"",
                    ),
                ),
                patch.object(provider_deployment, "_run_compose_down") as compose,
                self.assertRaisesRegex(
                    DeploymentOperationRejected,
                    "foreign or ambiguous session volume",
                ),
            ):
                stop_deployment(repository, "production")

            compose.assert_not_called()

    def test_resource_inspection_accepts_only_owned_disposable_resources(self) -> None:
        project = "nmrpeak-production"
        provider_id = "a" * 64
        hf_id = "b" * 64
        chf_id = "c" * 64
        network_id = "d" * 64
        hf_volume = f"{project}_hf-session"
        chf_volume = f"{project}_chf-session"
        volume_records = [
            {
                "Name": name,
                "Driver": "local",
                "Options": {
                    "device": "tmpfs",
                    "o": "size=1m,uid=65532,gid=65532,mode=0700",
                    "type": "tmpfs",
                },
                "Labels": {
                    "com.docker.compose.project": project,
                    "com.docker.compose.volume": logical_name,
                },
            }
            for name, logical_name in (
                (hf_volume, "hf-session"),
                (chf_volume, "chf-session"),
            )
        ]
        network_record = [
            {
                "Id": network_id,
                "Name": f"{project}_default",
                "Driver": "bridge",
                "Labels": {
                    "com.docker.compose.project": project,
                    "com.docker.compose.network": "default",
                },
                "Containers": {
                    provider_id: {},
                    hf_id: {},
                    chf_id: {},
                },
            }
        ]
        outputs = (
            subprocess.CompletedProcess(
                (), 0, f"{hf_volume}\n{chf_volume}\n".encode(), b""
            ),
            subprocess.CompletedProcess((), 0, json.dumps(volume_records).encode(), b""),
            subprocess.CompletedProcess(
                (), 0, f"{provider_id}\n{hf_id}\n".encode(), b""
            ),
            subprocess.CompletedProcess(
                (), 0, f"{provider_id}\n{chf_id}\n".encode(), b""
            ),
            subprocess.CompletedProcess((), 0, f"{network_id}\n".encode(), b""),
            subprocess.CompletedProcess((), 0, json.dumps(network_record).encode(), b""),
        )
        services = {
            "provider": {"Id": provider_id},
            "hf-runner": {"Id": hf_id},
            "chf-runner": {"Id": chf_id},
        }
        with patch.object(
            provider_deployment,
            "_docker_command",
            side_effect=outputs,
        ):
            resources = provider_deployment._inspect_project_resources(
                Path("/usr/bin/docker"),
                "production",
                services,
            )

        self.assertEqual(resources, (hf_volume, chf_volume, f"{project}_default"))

    def test_down_confirms_no_project_residue_after_compose(self) -> None:
        with TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            state = repository / "secrets/deployments/production"
            state.mkdir(parents=True, mode=0o700)
            state.chmod(0o700)
            with (
                patch.object(
                    provider_deployment,
                    "_inspect_project_containers",
                    side_effect=({}, {}),
                ) as containers,
                patch.object(
                    provider_deployment,
                    "_inspect_project_resources",
                    side_effect=((), ()),
                ) as resources,
                patch.object(provider_deployment, "_run_compose_down") as compose,
            ):
                stop_deployment(repository, "production")

            self.assertEqual(containers.call_count, 2)
            self.assertEqual(resources.call_count, 2)
            compose.assert_called_once()

    def test_down_stops_provider_then_runners_and_removes_sessions(self) -> None:
        calls: list[tuple[str, ...]] = []

        def docker_command(
            _docker: Path,
            arguments: tuple[str, ...],
            *,
            timeout: int,
        ) -> subprocess.CompletedProcess[bytes]:
            calls.append(arguments)
            return subprocess.CompletedProcess((), 0, b"", b"")

        with (
            patch.object(
                provider_deployment,
                "_docker_command",
                side_effect=docker_command,
            ),
            patch.object(
                provider_deployment,
                "_read_committed_template",
                return_value=(ROOT / "compose/provider-teardown.yml").read_bytes(),
            ),
        ):
            provider_deployment._run_compose_down(
                Path("/usr/bin/docker"),
                ROOT,
                "production",
            )

        self.assertEqual(calls[0][-4:], ("stop", "--timeout", "600", "provider"))
        self.assertEqual(
            calls[1][-5:],
            ("stop", "--timeout", "20", "hf-runner", "chf-runner"),
        )
        self.assertEqual(
            calls[2][-5:],
            ("down", "--timeout", "20", "--remove-orphans", "--volumes"),
        )
        teardown = (ROOT / "compose/provider-teardown.yml").read_text()
        for retained_volume in (
            "provider-journal",
            "provider-identity-lock",
            "hf-checkpoint",
            "chf-checkpoint",
        ):
            self.assertNotIn(retained_volume, teardown)

    def test_private_ca_generation_removal_preserves_referenced_and_neighboring_state(
        self,
    ) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)
            materialize_deployment_plan(repository, "production", plan)
            generation_id = plan.generation.frozen_generation_id
            generation = (
                repository
                / "secrets/deployments/production/generations"
                / generation_id.removeprefix("sha256:")
            )
            neighbor = generation.parent / ("f" * 64)
            neighbor.mkdir(mode=0o700)
            provider_config = repository / "config/deployments/production/provider.toml"
            provider_config.write_bytes(
                provider_config.read_bytes()
                .replace(b"https://api.example.test", b"https://nmr.localhost:10443")
                .replace(b'topology = "web"', b'topology = "dev-local"')
                .replace(b"[server_a]\n", b"[server_a]\nuse_private_ca = true\n")
            )
            with (
                patch.object(
                    provider_deployment,
                    "_inspect_project_containers",
                    return_value={},
                ),
                patch.object(
                    provider_deployment,
                    "_journal_generation_ids",
                    return_value=(generation_id,),
                ),
                self.assertRaisesRegex(
                    DeploymentOperationRejected,
                    "still references",
                ),
            ):
                remove_frozen_generation(
                    repository,
                    "production",
                    generation_id,
                    generation_id,
                )
            self.assertTrue(generation.is_dir())

            with (
                patch.object(
                    provider_deployment,
                    "_inspect_project_containers",
                    return_value={},
                ),
                patch.object(
                    provider_deployment,
                    "_journal_generation_ids",
                    return_value=(),
                ),
            ):
                remove_frozen_generation(
                    repository,
                    "production",
                    generation_id,
                    generation_id,
                )
            self.assertFalse(generation.exists())
            self.assertTrue(neighbor.is_dir())

    def test_journal_inventory_helper_has_only_read_only_journal_authority(
        self,
    ) -> None:
        image = LocalImage("sha256:" + "1" * 64, "sha256:" + "2" * 64)
        generation_id = "sha256:" + "3" * 64
        output = canonical_json_bytes(
            {
                "schema_id": "nmrpeak.journal_generation_inventory.v1",
                "frozen_generation_ids": [generation_id],
            }
        ) + b"\n"
        captured: list[tuple[str, ...]] = []

        def docker_command(
            _docker: Path,
            arguments: tuple[str, ...],
            *,
            timeout: int,
        ) -> subprocess.CompletedProcess[bytes]:
            captured.append(arguments)
            self.assertEqual(timeout, 300)
            return subprocess.CompletedProcess((), 0, output, b"")

        with (
            patch.object(
                provider_deployment,
                "inspect_provider_journal_volume",
                return_value=("nmrpeak-production-journal-v1", ()),
            ),
            patch.object(
                provider_deployment,
                "_resolve_provider_image",
                return_value=image,
            ),
            patch.object(
                provider_deployment,
                "_docker_command",
                side_effect=docker_command,
            ),
        ):
            references = provider_deployment._journal_generation_ids(
                Path("/usr/bin/docker"),
                ROOT,
                "production",
                "provider:nmrpeak",
                "sha256:" + "d" * 64,
                {},
            )

        self.assertEqual(references, (generation_id,))
        arguments = captured[0]
        self.assertIn("--network", arguments)
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        mount = arguments[arguments.index("--mount") + 1]
        self.assertEqual(
            mount,
            "type=volume,src=nmrpeak-production-journal-v1,"
            "dst=/var/lib/nmrpeak-provider,readonly",
        )
        self.assertNotIn("credential", " ".join(arguments))
        self.assertNotIn("checkpoint", " ".join(arguments))

    def test_journal_retirement_requires_exact_confirmation_before_engine_use(
        self,
    ) -> None:
        with render_repository() as repository, patch.object(
            provider_deployment,
            "_inspect_project_containers",
        ) as inspect:
            with self.assertRaisesRegex(
                DeploymentOperationRejected,
                "full volume name",
            ):
                retire_provider_journal(repository, "production", "wrong")
        inspect.assert_not_called()

    def test_identity_lock_holder_keeps_stdin_and_has_exact_authority(self) -> None:
        class Holder:
            def __init__(self) -> None:
                self.stdin = BytesIO()
                self.stdout = BytesIO(b"READY\n")

            def poll(self) -> None:
                return None

            def wait(self, *, timeout: int) -> int:
                return 0

        holder = Holder()
        image = LocalImage("sha256:" + "1" * 64, "sha256:" + "2" * 64)
        with TemporaryDirectory() as temporary:
            container_id_path = Path(temporary) / "holder.cid"
            container_id_path.write_text("a" * 64 + "\n", encoding="ascii")
            with (
                patch.object(
                    provider_deployment.subprocess,
                    "Popen",
                    return_value=holder,
                ) as popen,
                patch.object(
                    provider_deployment.select,
                    "select",
                    return_value=([holder.stdout], [], []),
                ),
                patch.object(
                    provider_deployment,
                    "_holder_container_exists",
                    return_value=False,
                ),
                provider_deployment._run_held_provider_identity_lock(
                    Path("/usr/bin/docker"),
                    "nmrpeak-provider-lock-test",
                    "provider:nmrpeak",
                    image,
                    container_id_path,
                ),
            ):
                pass

        arguments = popen.call_args.args[0]
        self.assertIn("--interactive", arguments)
        self.assertEqual(
            arguments[arguments.index("--cidfile") + 1],
            str(container_id_path),
        )
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertIn(
            "type=volume,src=nmrpeak-provider-lock-test,"
            "dst=/run/nmrpeak-provider-lock,readonly",
            arguments,
        )
        self.assertNotIn("credential", " ".join(arguments))
        self.assertNotIn("checkpoint", " ".join(arguments))
        self.assertTrue(holder.stdin.closed)

    def test_wedged_identity_lock_holder_removes_container_then_reaps(self) -> None:
        class WedgedHolder:
            def __init__(self) -> None:
                self.stdin = BytesIO()

            def wait(self, *, timeout: int) -> int:
                raise subprocess.TimeoutExpired("docker run", timeout)

        holder = WedgedHolder()
        with TemporaryDirectory() as temporary:
            container_id_path = Path(temporary) / "holder.cid"
            container_id_path.write_text("b" * 64 + "\n", encoding="ascii")
            with (
                patch.object(
                    provider_deployment,
                    "_remove_identity_lock_holder_container",
                ) as remove,
                patch.object(provider_deployment, "_reap_docker_client") as reap,
                self.assertRaisesRegex(
                    DeploymentOperationRejected,
                    "forced container removal",
                ),
            ):
                provider_deployment._stop_identity_lock_holder(
                    holder,
                    Path("/usr/bin/docker"),
                    container_id_path,
                )

        self.assertTrue(holder.stdin.closed)
        remove.assert_called_once_with(Path("/usr/bin/docker"), "b" * 64)
        reap.assert_called_once_with(holder)

    def test_forced_holder_cleanup_stops_removes_and_proves_absence(self) -> None:
        container_id = "c" * 64
        commands: list[tuple[str, ...]] = []

        def docker_command(
            _docker: Path,
            arguments: tuple[str, ...],
            *,
            timeout: int,
        ) -> subprocess.CompletedProcess[bytes]:
            commands.append(arguments)
            self.assertEqual(timeout, 15)
            return subprocess.CompletedProcess(
                (),
                0,
                container_id.encode("ascii") + b"\n",
                b"",
            )

        with (
            patch.object(
                provider_deployment,
                "_holder_container_exists",
                side_effect=(True, True, False),
            ),
            patch.object(
                provider_deployment,
                "_docker_command",
                side_effect=docker_command,
            ),
        ):
            provider_deployment._remove_identity_lock_holder_container(
                Path("/usr/bin/docker"),
                container_id,
            )

        self.assertEqual(
            commands,
            [
                ("container", "stop", "--time", "5", container_id),
                ("container", "rm", "--force", container_id),
            ],
        )

    def test_missing_holder_identity_still_reaps_the_docker_client(self) -> None:
        class WedgedHolder:
            def __init__(self) -> None:
                self.stdin = BytesIO()

            def wait(self, *, timeout: int) -> int:
                raise subprocess.TimeoutExpired("docker run", timeout)

        holder = WedgedHolder()
        with TemporaryDirectory() as temporary, patch.object(
            provider_deployment,
            "_reap_docker_client",
        ) as reap, self.assertRaisesRegex(
            DeploymentOperationRejected,
            "identity is unavailable",
        ):
            provider_deployment._stop_identity_lock_holder(
                holder,
                Path("/usr/bin/docker"),
                Path(temporary) / "missing.cid",
            )

        reap.assert_called_once_with(holder)

    def test_journal_retirement_removes_only_after_empty_complete_inventory(
        self,
    ) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)
            materialize_deployment_plan(repository, "production", plan)
            credential = (
                repository
                / "secrets/deployments/production/signing.private.json"
            )
            credential.write_bytes(credential_bytes(Ed25519PrivateKey.generate()))
            credential.chmod(0o600)
            journal = "nmrpeak-production-journal-v1"
            image = LocalImage("sha256:" + "1" * 64, "sha256:" + "2" * 64)

            for inventory, message in (
                (AttemptInventoryReadFailed(object()), "complete Attempt inventory"),
                (AttemptInventory((object(),)), "in-progress Attempts"),
            ):
                with (
                    patch.object(
                        provider_deployment,
                        "_inspect_project_containers",
                        return_value={},
                    ),
                    patch.object(
                        provider_deployment,
                        "inspect_provider_journal_volume",
                        return_value=(journal, ()),
                    ),
                    patch.object(
                        provider_deployment,
                        "inspect_provider_identity_lock_volume",
                        return_value="nmrpeak-provider-lock-test",
                    ),
                    patch.object(
                        provider_deployment,
                        "_resolve_provider_image",
                        return_value=image,
                    ),
                    patch.object(
                        provider_deployment,
                        "_held_provider_identity_lock",
                        return_value=nullcontext(),
                    ),
                    patch.object(
                        provider_deployment,
                        "read_attempt_inventory",
                        return_value=inventory,
                    ),
                    patch.object(
                        provider_deployment,
                        "remove_provider_journal_volume",
                    ) as remove,
                    self.assertRaisesRegex(DeploymentOperationRejected, message),
                ):
                    retire_provider_journal(repository, "production", journal)
                remove.assert_not_called()

            with (
                patch.object(
                    provider_deployment,
                    "_inspect_project_containers",
                    return_value={},
                ),
                patch.object(
                    provider_deployment,
                    "inspect_provider_journal_volume",
                    return_value=(journal, ()),
                ),
                patch.object(
                    provider_deployment,
                    "inspect_provider_identity_lock_volume",
                    return_value="nmrpeak-provider-lock-test",
                ),
                patch.object(
                    provider_deployment,
                    "_resolve_provider_image",
                    return_value=image,
                ),
                patch.object(
                    provider_deployment,
                    "_held_provider_identity_lock",
                    return_value=nullcontext(),
                ) as held,
                patch.object(
                    provider_deployment,
                    "read_attempt_inventory",
                    return_value=AttemptInventory(()),
                ),
                patch.object(
                    provider_deployment,
                    "remove_provider_journal_volume",
                    return_value=journal,
                ) as remove,
            ):
                self.assertEqual(
                    retire_provider_journal(repository, "production", journal),
                    journal,
                )
            held.assert_called_once_with(
                Path("/usr/bin/docker"),
                "nmrpeak-provider-lock-test",
                "provider:nmrpeak",
                image,
            )
            remove.assert_called_once_with(
                Path("/usr/bin/docker"),
                "production",
                "provider:nmrpeak",
                "sha256:1961d55ea350b47586f7208f5ab84a2be2214bdfe3700033d41c3a07e11d05ce",
            )


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


def write_api_credential(
    api_root: Path,
    content: bytes,
    *,
    profile: str = "run",
) -> Path:
    path = api_root / f"secrets/{profile}/providers/nmrpeak/signing.private.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    path.chmod(0o600)
    return path


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


def compose_for_environment(
    deployment: str,
    environment: dict[str, str],
    *,
    localhost: bool = False,
) -> dict[str, object]:
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
    provider_mounts[7]["source"] = environment["INTERPRETER_CONFIG_PATH"]
    if localhost:
        services["provider"]["extra_hosts"] = ["nmr.localhost=host-gateway"]
        provider_mounts.append(
            {
                "type": "bind",
                "source": environment["LOCALHOST_CA_CERTIFICATE_PATH"],
                "target": "/run/config/nmrpeak-provider/server-a-ca.crt",
                "read_only": True,
            }
        )
    return document


def test_plan(repository: Path) -> DeploymentPlan:
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
            side_effect=lambda _, deployment, environment, __, *, localhost: (
                compose_for_environment(
                    deployment,
                    environment,
                    localhost=localhost,
                )
            ),
        ),
    ):
        return render_deployment_plan(repository, "production")


def ready_services(plan: DeploymentPlan) -> dict[str, dict[str, object]]:
    return {
        service: {
            "Image": plan.compose["services"][service]["image"],
            "State": {
                "Status": "running",
                **({"Health": {"Status": "healthy"}} if service == "provider" else {}),
            },
        }
        for service in ("provider", "hf-runner", "chf-runner")
    }


if __name__ == "__main__":
    unittest.main()
