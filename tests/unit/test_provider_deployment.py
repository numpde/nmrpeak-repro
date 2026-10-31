"""Prove initialization publishes only a new literal deployment config."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from hashlib import sha256
from io import BytesIO, StringIO
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
    materialize_deployment_plan,
    remove_frozen_generation,
    retire_provider_journal,
    render_deployment_plan,
    show_provider_logs,
    start_deployment,
    stop_deployment,
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
from tests.unit.test_provider_startup_inputs import credential_bytes
from tests.unit.test_deployment_runtime import DEFINITION
from nmrpeak_provider.deployment_runtime import project_provider_inputs
from provider_core.deployment_binding import load_deployment_bindings
from provider_core.deployment_route import NamedRouteVolume, render_api_route


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
    def setUp(self) -> None:
        self.docker_host = patch.dict("os.environ", {"NMRPEAK_DOCKER_HOST": "unix:///tmp/nmrpeak-test.sock"})
        self.docker_host.start()
        self.addCleanup(self.docker_host.stop)

    def test_unconfirmed_reader_creation_never_dispatches_start(self) -> None:
        with patch.object(provider_deployment, "_docker_command", side_effect=TimeoutError("creation unknown")) as command, \
             self.assertRaises(provider_deployment.MaintenanceInvocationUnsettled):
            provider_deployment._run_owned_reader(Path("/usr/bin/docker"), "reader", "token", ("image",))
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[1][0], "create")

    def test_unconfirmed_runtime_validation_retains_its_captured_inputs(self) -> None:
        with TemporaryDirectory() as temporary:
            retained = Path(temporary) / "retained"
            retained.mkdir()
            with (
                patch.object(provider_deployment, "mkdtemp", return_value=str(retained)),
                patch.object(provider_deployment, "_publish_retained_tree", return_value=retained),
                patch.object(provider_deployment, "_docker_command", side_effect=TimeoutError("validation timed out")),
                patch.object(provider_deployment, "_remove_archive_invocation", side_effect=OSError("retirement unknown")),
                self.assertRaises(provider_deployment.MaintenanceInvocationUnsettled),
            ):
                provider_deployment._validate_captured_runtime(Path("/usr/bin/docker"), "image", {"api.json": b"{}"})
            self.assertTrue(retained.is_dir())

    def test_maintenance_capture_accepts_fractional_api_deadlines(self) -> None:
        from types import SimpleNamespace
        from tests.unit.test_runtime_config_v2 import POLICY
        from tests.unit.test_deployment_topology import BINDING
        from nmrpeak_provider.deployment_runtime import api_facts

        api = dict(POLICY.api, exchange_seconds=0.5)
        policy = SimpleNamespace(api=api, shared_project="nmrpeak-fixture", shared_gid=1234)
        facts = api_facts(api) | {"input_paths": [api["credential_file"]], "credential_file": api["credential_file"]}
        projection = facts | {"shared": BINDING["shared"]}
        files_seen = {}

        def capture(_parent, _identity, files):
            files_seen.update(files)
            return Path("/tmp/captured-api")

        expected = {key: facts[key] for key in ("provider_ref", "api_origin", "api_topology", "journal_authority_id")}
        with (
            patch.object(provider_deployment, "_journal_binding", return_value=(policy, None, facts)),
            patch.object(provider_deployment, "_read_regular_file", return_value=b"provider config"),
            patch.object(provider_deployment, "_read_capture_source", return_value=b"captured"),
            patch.object(provider_deployment, "_contained_shared_projection", return_value=projection),
            patch.object(provider_deployment, "_publish_retained_tree", side_effect=capture),
            patch.object(provider_deployment, "_validate_captured_runtime", return_value=expected | {
                "proxy_socket": BINDING["shared"]["proxy_socket"],
            }),
        ):
            with provider_deployment._held_maintenance_api_route(
                Path("/usr/bin/docker"), Path("/repo"), "production", IMAGES["provider"],
                "provider:nmrpeak", facts["journal_authority_id"],
            ):
                stored = json.loads(files_seen["api.json"])
                self.assertEqual(stored["exchange_seconds"], 0.5)
                self.assertNotEqual(stored["credential_file"], api["credential_file"])

    def test_maintenance_capture_survives_unconfirmed_reader_retirement(self) -> None:
        from types import SimpleNamespace
        from tests.unit.test_runtime_config_v2 import POLICY
        from tests.unit.test_deployment_topology import BINDING
        from nmrpeak_provider.deployment_runtime import api_facts

        policy = SimpleNamespace(api=POLICY.api, shared_project="nmrpeak-fixture", shared_gid=1234)
        facts = api_facts(policy.api) | {
            "input_paths": [policy.api["credential_file"]], "credential_file": policy.api["credential_file"],
        }
        projection = facts | {"shared": BINDING["shared"]}
        expected = {key: facts[key] for key in ("provider_ref", "api_origin", "api_topology", "journal_authority_id")}
        with TemporaryDirectory() as temporary:
            retained = Path(temporary) / "retained"
            retained.mkdir()
            with (
                patch.object(provider_deployment, "mkdtemp", return_value=str(retained)),
                patch.object(provider_deployment, "_journal_binding", return_value=(policy, None, facts)),
                patch.object(provider_deployment, "_read_regular_file", return_value=b"provider config"),
                patch.object(provider_deployment, "_read_capture_source", return_value=b"captured"),
                patch.object(provider_deployment, "_contained_shared_projection", return_value=projection),
                patch.object(provider_deployment, "_publish_retained_tree", return_value=retained),
                patch.object(provider_deployment, "_validate_captured_runtime", return_value=expected | {
                    "proxy_socket": BINDING["shared"]["proxy_socket"],
                }),
                self.assertRaises(provider_deployment.MaintenanceInvocationUnsettled),
            ):
                with provider_deployment._held_maintenance_api_route(
                    Path("/usr/bin/docker"), Path("/repo"), "production", IMAGES["provider"],
                    "provider:nmrpeak", facts["journal_authority_id"],
                ):
                    raise provider_deployment.MaintenanceInvocationUnsettled("reader cleanup unconfirmed")
            self.assertTrue(retained.is_dir())

    def test_journal_client_checks_shared_volume_before_borrowing(self) -> None:
        from tests.unit.test_deployment_topology import BINDING

        shared = BINDING["shared"]
        volume = shared["mounts"][0]["source"]
        image = LocalImage("sha256:" + "1" * 64, "sha256:" + "2" * 64)
        calls = []

        def docker_command(_docker, arguments, *, timeout):
            calls.append(arguments)
            if arguments[:2] == ("volume", "inspect"):
                return subprocess.CompletedProcess((), 0, json.dumps([{"Name": volume}]).encode(), b"")
            if arguments[0] == "create":
                return subprocess.CompletedProcess((), 0, ("d" * 64 + "\n").encode(), b"")
            return subprocess.CompletedProcess((), 0, b"ok", b"")

        with patch.object(provider_deployment, "_docker_command", side_effect=docker_command):
            result = provider_deployment._run_journal_api_client(
                Path("/usr/bin/docker"), image, BINDING, Path("/tmp/capture"), "module", (),
            )
        self.assertEqual(result, b"ok")
        self.assertEqual(calls[0], ("volume", "inspect", volume))
        self.assertIn("--group-add", calls[1])
        self.assertIn(str(shared["socket_group_id"]), calls[1])
        self.assertTrue(any("readonly,volume-nocopy" in value and volume in value for value in calls[1]))

        with patch.object(provider_deployment, "_docker_command", return_value=subprocess.CompletedProcess(
            (), 0, b"[]", b"",
        )) as command, self.assertRaisesRegex(DeploymentOperationRejected, "exact shared proxy volume"):
            provider_deployment._run_journal_api_client(
                Path("/usr/bin/docker"), image, BINDING, Path("/tmp/capture"), "module", (),
            )
        self.assertEqual(command.call_count, 1)

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
                binding_projection: dict,
            ) -> dict[str, object]:
                renders.append(environment)
                return compose_for_environment(
                    deployment, environment, binding_projection=binding_projection,
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
                    renders[1]["PROVIDER_RUNTIME_DIRECTORY"],
                    renders[3]["PROVIDER_RUNTIME_DIRECTORY"],
                )
                self.assertEqual(
                    renders[1]["FROZEN_GENERATION_PATH"],
                    renders[3]["FROZEN_GENERATION_PATH"],
                )

            preview = parse_canonical_json_bytes(deployment_plan_bytes(plan))
            self.assertEqual(preview["kind"], "read_only_preview")
            self.assertEqual(
                preview["frozen_generation_id"],
                plan.generation.frozen_generation_id,
            )
            self.assertEqual(preview["runtime_config_id"], plan.runtime_config_id)
            self.assertEqual(
                set(preview["artifacts"]),
                {"provider.toml", "runtime_files", "frozen/manifest.json", "frozen/files"},
            )
            self.assertEqual(
                Path(renders[1]["FROZEN_GENERATION_PATH"]).parent.name,
                plan.generation.frozen_generation_id.removeprefix("sha256:"),
            )
            self.assertEqual(
                Path(renders[1]["PROVIDER_RUNTIME_DIRECTORY"]).name,
                plan.runtime_config_id.removeprefix("sha256:"),
            )
            self.assertFalse((repository / "secrets/deployments/production/runtime-configs").exists())
            self.assertFalse((repository / "secrets/deployments/production/generations").exists())

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
            self.assertEqual(first[0].read_bytes(), plan.runtime_files["provider.toml"])
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

    def test_start_admits_every_input_before_compose_and_readiness_proof(self) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)
            preview_sha256 = sha256(deployment_plan_bytes(plan) + b"\n").hexdigest()
            events: list[str] = []
            with (
                patch.object(provider_deployment, "render_deployment_plan", return_value=plan),
                patch.object(provider_deployment, "_materialize_locked", side_effect=lambda *_: events.append("materialize")),
                patch.object(provider_deployment, "verify_hf_checkpoint", side_effect=lambda *_args, **_kwargs: events.append("hf")),
                patch.object(provider_deployment, "verify_chf_checkpoint", side_effect=lambda *_args, **_kwargs: events.append("chf")),
                patch.object(provider_deployment, "ensure_provider_state_volumes", side_effect=lambda *_: events.append("provider_volumes")),
                patch.object(provider_deployment, "_inspect_project_containers", side_effect=({}, ready_services(plan))),
                patch.object(provider_deployment, "_admit_session_volumes", side_effect=lambda *_: events.append("session_admission")),
                patch.object(provider_deployment, "_run_compose_plan", side_effect=lambda *_: events.append("compose_up")),
            ):
                self.assertEqual(start_deployment(repository, "production", expected_plan_sha256=preview_sha256), plan)
            self.assertEqual(events, ["session_admission", "materialize", "hf", "chf", "provider_volumes", "compose_up"])


    def test_start_refuses_session_volume_drift_before_changes(self) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)
            with (
                patch.object(provider_deployment, "render_deployment_plan", return_value=plan),
                patch.object(provider_deployment, "_inspect_project_containers", return_value={}),
                patch.object(provider_deployment, "_admit_session_volumes", side_effect=DeploymentOperationRejected("volume drift")),
                patch.object(provider_deployment, "_materialize_locked") as materialize,
                patch.object(provider_deployment, "_run_compose_plan") as compose,
                self.assertRaisesRegex(DeploymentOperationRejected, "volume drift"),
            ):
                start_deployment(repository, "production", expected_plan_sha256=sha256(deployment_plan_bytes(plan) + b"\n").hexdigest())
            materialize.assert_not_called()
            compose.assert_not_called()

    def test_start_session_volume_admission_checks_actual_engine_grant(self) -> None:
        name = "nmrpeak-production_hf-session"
        record = {"Name": name, "Driver": "local", "Options": dict(provider_deployment._SESSION_VOLUME_OPTIONS),
                  "Labels": {"com.docker.compose.project": "nmrpeak-production", "com.docker.compose.volume": "hf-session"}}
        def inspect(candidate, attachment=b"", services=None):
            def command(_docker, arguments, **_kwargs):
                if arguments[:2] == ("volume", "ls"):
                    output = (name + "\n").encode()
                elif arguments[:2] == ("volume", "inspect"):
                    output = json.dumps([candidate]).encode()
                else:
                    output = attachment
                return subprocess.CompletedProcess(arguments, 0, output, b"")
            with patch.object(provider_deployment, "_docker_command", side_effect=command):
                provider_deployment._admit_session_volumes(Path("/usr/bin/docker"), "production", services or {})
        inspect(record)
        for altered in (
            {**record, "Driver": "other"},
            {**record, "Options": {**record["Options"], "o": "size=2m"}},
            {**record, "Labels": {"com.docker.compose.project": "wrong", "com.docker.compose.volume": "hf-session"}},
        ):
            with self.subTest(altered=altered), self.assertRaises(DeploymentOperationRejected):
                inspect(altered)
        with self.assertRaisesRegex(DeploymentOperationRejected, "foreign container"):
            inspect(record, ("a" * 64 + "\n").encode())
        provider_id = "a" * 64
        mount = {"Type": "volume", "Name": name, "Destination": "/run/nmrpeak-provider/hf", "RW": True}
        services = {"provider": {"Id": provider_id, "Mounts": [mount]}}
        inspect(record, (provider_id + "\n").encode(), services)
        services["provider"]["Mounts"].append({**mount, "Destination": "/extra"})
        with self.assertRaisesRegex(DeploymentOperationRejected, "unexpected container grant"):
            inspect(record, (provider_id + "\n").encode(), services)
        services["provider"]["Mounts"].pop()
        mount["Destination"] = "/wrong"
        with self.assertRaisesRegex(DeploymentOperationRejected, "unexpected container grant"):
            inspect(record, (provider_id + "\n").encode(), services)

    def test_start_rejects_changed_preview_before_materialization_or_engine_effects(self) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)
            with (
                patch.object(provider_deployment, "render_deployment_plan", return_value=plan),
                patch.object(provider_deployment, "_materialize_locked") as materialize,
                patch.object(provider_deployment, "ensure_provider_state_volumes") as volumes,
                patch.object(provider_deployment, "_run_compose_plan") as compose_up,
                self.assertRaisesRegex(DeploymentOperationRejected, "differs from reviewed preview"),
            ):
                start_deployment(
                    repository,
                    "production",
                    expected_plan_sha256="0" * 64,
                )
            materialize.assert_not_called()
            volumes.assert_not_called()
            compose_up.assert_not_called()

    def test_start_rejects_malformed_preview_digest_before_state_access(self) -> None:
        with render_repository() as repository:
            with (
                patch.object(provider_deployment, "_ensure_deployment_state_root") as state_root,
                patch.object(provider_deployment, "render_deployment_plan") as render,
                self.assertRaisesRegex(DeploymentOperationRejected, "64 lowercase hexadecimal"),
            ):
                start_deployment(
                    repository,
                    "production",
                    expected_plan_sha256="SHA256:" + "A" * 64,
                )
            state_root.assert_not_called()
            render.assert_not_called()

    def test_config_output_digest_is_the_expected_start_digest(self) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)
            output = StringIO()
            with (
                patch.object(provider_deployment, "render_deployment_plan", return_value=plan),
                redirect_stdout(output),
            ):
                self.assertEqual(provider_deployment.main(["config", "production"]), 0)
            self.assertEqual(
                sha256(output.getvalue().encode("utf-8")).hexdigest(),
                sha256(deployment_plan_bytes(plan) + b"\n").hexdigest(),
            )

    def test_start_stops_before_engine_effects_when_runtime_materialization_fails(self) -> None:
        with render_repository() as repository:
            plan = test_plan(repository)
            failure = DeploymentOperationRejected("Captured provider inputs could not be published")
            with (
                patch.object(provider_deployment, "render_deployment_plan", return_value=plan),
                patch.object(provider_deployment, "_inspect_project_containers", return_value={}),
                patch.object(provider_deployment, "_admit_session_volumes"),
                patch.object(provider_deployment, "_materialize_locked", side_effect=failure),
                patch.object(provider_deployment, "verify_hf_checkpoint") as verify_hf,
                patch.object(provider_deployment, "verify_chf_checkpoint") as verify_chf,
                patch.object(provider_deployment, "ensure_provider_state_volumes") as ensure_volumes,
                patch.object(provider_deployment, "_run_compose_plan") as compose_up,
                self.assertRaisesRegex(DeploymentOperationRejected, "could not be published"),
            ):
                start_deployment(repository, "production",
                    expected_plan_sha256=sha256(deployment_plan_bytes(plan) + b"\n").hexdigest())
            verify_hf.assert_not_called()
            verify_chf.assert_not_called()
            ensure_volumes.assert_not_called()
            compose_up.assert_not_called()


    def test_interpreter_config_capture_keeps_exact_bounded_source_bytes(self) -> None:
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            directory = state / "openai-chat-completions.d"
            directory.mkdir(mode=0o700)
            endpoint = directory / "10-primary.toml"
            source = b'id="primary"\nbase_url="https://example.test"\napi_key="secret"\nmodel="model"\n'
            endpoint.write_bytes(source)
            endpoint.chmod(0o600)
            with patch.object(provider_deployment, "_read_acl",
                              return_value=provider_deployment._PRIVATE_WRITABLE_FILE_ACL):
                self.assertEqual(provider_deployment._capture_interpreter_inputs(state),
                                 {"interpreter/10-primary.toml": source})
            (directory / "README").write_text("unexpected", encoding="utf-8")
            with patch.object(provider_deployment, "_read_acl",
                              return_value=provider_deployment._PRIVATE_WRITABLE_FILE_ACL), \
                 self.assertRaisesRegex(DeploymentOperationRejected, "non-TOML entry"):
                provider_deployment._capture_interpreter_inputs(state)


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

        compose_failure = DeploymentOperationRejected(
            "The Docker provider deployment operation exited with status 1: "
            "container provider is unhealthy"
        )
        with patch.object(
            provider_deployment,
            "_docker_command",
            side_effect=compose_failure,
        ), self.assertRaises(DeploymentOperationRejected) as raised:
            provider_deployment._run_compose_plan(
                Path("/usr/bin/docker"),
                repository,
                "production",
                plan,
            )
        rendered = str(raised.exception)
        self.assertIn("Cannot confirm provider deployment 'production' is ready", rendered)
        self.assertIn("Containers and session volumes may remain", rendered)
        self.assertIn("provider/deployment/status DEPLOYMENT=production", rendered)
        self.assertIn("provider/logs DEPLOYMENT=production", rendered)
        self.assertIs(raised.exception.__cause__, compose_failure)

    def test_docker_rejection_keeps_all_bounded_printable_diagnostic_lines(self) -> None:
        diagnostic = b"progress\n\x1b[31mcontainer provider is unhealthy\x1b[0m\n"
        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess((), 1, b"", diagnostic),
        ), self.assertRaises(DeploymentOperationRejected) as raised:
            provider_deployment._docker_command(
                Path("/usr/bin/docker"),
                ("compose", "up"),
                timeout=720,
            )
        self.assertEqual(
            str(raised.exception),
            "The Docker provider deployment operation exited with status 1: "
            "progress container provider is unhealthy",
        )

    def test_acl_rejection_preserves_exit_and_redacted_stderr(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
            (), 7, b"", b"ACL entry rejected\nPermission denied; Bearer hidden")), self.assertRaises(DeploymentOperationRejected) as raised:
            provider_deployment._run_acl_command(Path("/usr/bin/setfacl"), "--", "/tmp/input")
        message = str(raised.exception)
        self.assertIn("setfacl", message)
        self.assertIn("exit=7", message)
        self.assertIn("ACL entry rejected", message)
        self.assertIn("Permission denied", message)
        self.assertNotIn("hidden", message)

    def test_docker_timeout_preserves_partial_stderr_and_effect_uncertainty(self) -> None:
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(
            ("docker", "compose", "up"), 30, stderr=b"Image unpack failed; api_key=hidden")), self.assertRaises(DeploymentOperationRejected) as raised:
            provider_deployment._docker_command(Path("/usr/bin/docker"), ("compose", "up"), timeout=30)
        message = str(raised.exception)
        self.assertIn("effects are unknown", message)
        self.assertIn("Image unpack failed", message)
        self.assertNotIn("hidden", message)

    def test_cli_names_missing_and_forbidden_operation_options(self) -> None:
        cases = (
            (["config", "localhost", "--replace"], "unrecognized arguments: --replace"),
            (["down", "localhost", "--confirm", "x"], "down does not accept --confirm"),
            (
                ["generation-remove", "localhost", "--nmr-api-v1", "/tmp/api"],
                "unrecognized arguments: --nmr-api-v1 /tmp/api",
            ),
            (["init", "localhost", "--replace"], "unrecognized arguments: --replace"),
            (
                ["journal-retire", "localhost", "--replace"],
                "unrecognized arguments: --replace",
            ),
            (["logs", "localhost", "--confirm", "x"], "logs does not accept --confirm"),
            (["status", "localhost", "--replace"], "unrecognized arguments: --replace"),
            (["up", "localhost", "--expected-plan-sha256", "0" * 64, "--replace"],
             "unrecognized arguments: --replace"),
            (["up", "localhost"], "up requires --expected-plan-sha256"),
            (["config", "localhost", "--expected-plan-sha256", "0" * 64],
             "config does not accept --expected-plan-sha256"),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                stderr = StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit):
                    provider_deployment.main(arguments)
                self.assertIn(expected, stderr.getvalue())

    def test_deployment_renderer_preserves_redacted_os_cause(self) -> None:
        cause = DeploymentOperationRejected(
            "The Docker provider deployment operation exited with status 1: "
            "container provider is unhealthy"
        )
        failure = DeploymentOperationRejected(
            "Cannot confirm provider deployment 'localhost' is ready"
        )
        failure.__cause__ = cause
        cause.__cause__ = OSError("Daemon socket permission denied; Bearer actual-daemon-secret")

        stderr = StringIO()
        with (
            patch.object(
                provider_deployment,
                "start_deployment",
                side_effect=failure,
            ),
            redirect_stderr(stderr),
        ):
            status = provider_deployment.main(
                ["up", "localhost", "--expected-plan-sha256", "0" * 64]
            )

        self.assertEqual(status, 2)
        rendered = stderr.getvalue()
        self.assertIn("Provider deployment startup failed for 'localhost'", rendered)
        self.assertIn("DeploymentOperationRejected: The Docker provider deployment operation", rendered)
        self.assertIn("Daemon socket permission denied", rendered)
        self.assertNotIn("actual-daemon-secret", rendered)

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
                "--host",
                "unix:///tmp/nmrpeak-test.sock",
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
                "NMRPEAK_DOCKER_HOST": "unix:///tmp/nmrpeak-test.sock",
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

    def test_down_retries_owned_api_volume_after_containers_are_gone(self) -> None:
        for deployment in ("production", "x" * 60):
            with self.subTest(deployment=deployment), TemporaryDirectory() as temporary:
                repository = Path(temporary).resolve()
                project = f"nmrpeak-{deployment}"
                definition = repository / "bindings.toml"
                definition.write_text(DEFINITION)
                binding = load_deployment_bindings(definition).selected("first")
                route = render_api_route(binding, image="sha256:" + "a" * 64,
                    interpreter="/usr/local/bin/python3.12", namespace=project, uid=65532, gid=65532,
                    storage=NamedRouteVolume(), input_sources={}, service_name="api")
                volume, = route.compose["volumes"]
                remaining = [True]

                def docker_command(_docker, arguments, *, timeout):
                    if arguments[:2] == ("volume", "ls"):
                        output = (volume + "\n").encode() if remaining[0] else b""
                    elif arguments[:2] == ("volume", "inspect"):
                        output = json.dumps([{
                            "Name": volume, "Driver": "local", "Options": {},
                            "Labels": {"com.docker.compose.project": project,
                                       "com.docker.compose.volume": volume},
                        }]).encode()
                    elif arguments[0] in {"ps", "network"}:
                        output = b""
                    else:
                        self.fail(f"Unexpected Docker operation: {arguments[:2]}")
                    return subprocess.CompletedProcess((), 0, output, b"")

                def down(*_args, route_volumes):
                    self.assertEqual(route_volumes, (volume,))
                    remaining[0] = False

                state = repository / "secrets/deployments" / deployment
                state.mkdir(parents=True, mode=0o700)
                state.chmod(0o700)
                with patch.object(provider_deployment, "_inspect_project_containers", return_value={}), \
                     patch.object(provider_deployment, "_docker_command", side_effect=docker_command), \
                     patch.object(provider_deployment, "_run_compose_down", side_effect=down):
                    stop_deployment(repository, deployment)
                self.assertFalse(remaining[0])

    def test_down_refuses_multiple_orphan_api_route_volumes(self) -> None:
        project = "nmrpeak-production"
        volumes = [f"{project}-route-" + digit * 64 for digit in ("a", "b")]
        with TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            state = repository / "secrets/deployments/production"
            state.mkdir(parents=True, mode=0o700)
            state.chmod(0o700)
            with (
                patch.object(provider_deployment, "_inspect_project_containers", return_value={}),
                patch.object(
                    provider_deployment,
                    "_docker_command",
                    return_value=subprocess.CompletedProcess(
                        (), 0, ("\n".join(volumes) + "\n").encode(), b""
                    ),
                ),
                patch.object(provider_deployment, "_run_compose_down") as compose,
                self.assertRaisesRegex(
                    DeploymentOperationRejected, "foreign or ambiguous session volume"
                ),
            ):
                stop_deployment(repository, "production")
            compose.assert_not_called()

    def test_down_refuses_route_named_volume_with_foreign_compose_label(self) -> None:
        project = "nmrpeak-production"
        volume = f"{project}-route-" + "a" * 64

        def docker_command(_docker, arguments, *, timeout):
            if arguments[:2] == ("volume", "ls"):
                output = (volume + "\n").encode()
            elif arguments[:2] == ("volume", "inspect"):
                output = json.dumps([{
                    "Name": volume, "Driver": "local", "Options": {},
                    "Labels": {"com.docker.compose.project": project,
                               "com.docker.compose.volume": "foreign"},
                }]).encode()
            else:
                self.fail(f"Unexpected Docker operation: {arguments[:2]}")
            return subprocess.CompletedProcess((), 0, output, b"")

        with TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            state = repository / "secrets/deployments/production"
            state.mkdir(parents=True, mode=0o700)
            state.chmod(0o700)
            with patch.object(provider_deployment, "_inspect_project_containers", return_value={}), \
                 patch.object(provider_deployment, "_docker_command", side_effect=docker_command), \
                 patch.object(provider_deployment, "_run_compose_down") as compose, \
                 self.assertRaisesRegex(DeploymentOperationRejected, "volume ownership is invalid"):
                stop_deployment(repository, "production")
            compose.assert_not_called()

    def test_down_refuses_orphan_api_volume_with_foreign_borrower(self) -> None:
        project = "nmrpeak-production"
        volume = f"{project}-route-" + "a" * 64

        def docker_command(_docker, arguments, *, timeout):
            if arguments[:2] == ("volume", "ls"):
                output = (volume + "\n").encode()
            elif arguments[:2] == ("volume", "inspect"):
                output = json.dumps([{
                    "Name": volume, "Driver": "local", "Options": {},
                    "Labels": {"com.docker.compose.project": project,
                               "com.docker.compose.volume": volume},
                }]).encode()
            elif arguments[0] == "ps":
                output = ("b" * 64 + "\n").encode()
            else:
                self.fail(f"Unexpected Docker operation: {arguments[:2]}")
            return subprocess.CompletedProcess((), 0, output, b"")

        with TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            state = repository / "secrets/deployments/production"
            state.mkdir(parents=True, mode=0o700)
            state.chmod(0o700)
            with patch.object(provider_deployment, "_inspect_project_containers", return_value={}), \
                 patch.object(provider_deployment, "_docker_command", side_effect=docker_command), \
                 patch.object(provider_deployment, "_run_compose_down") as compose, \
                 self.assertRaisesRegex(DeploymentOperationRejected, "foreign attachment"):
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
                route_volumes=(),
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
                patch.object(provider_deployment, "_docker_command", return_value=subprocess.CompletedProcess((), 0, b"", b"")),
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
                route_volumes=(),
            )

        self.assertEqual(calls[0][-4:], ("stop", "--timeout", "600", "provider"))
        self.assertEqual(
            calls[1][-5:],
            ("stop", "--timeout", "20", "hf-runner", "chf-runner"),
        )
        self.assertEqual(
            calls[2][-4:],
            ("down", "--timeout", "20", "--volumes"),
        )
        teardown = (ROOT / "compose/provider-teardown.yml").read_text()
        for retained_volume in (
            "provider-journal",
            "provider-identity-lock",
            "hf-checkpoint",
            "chf-checkpoint",
        ):
            self.assertNotIn(retained_volume, teardown)

    def test_generation_removal_preserves_referenced_and_neighboring_state(
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
            held = False

            @contextmanager
            def identity_lock(*_args):
                nonlocal held
                held = True
                try:
                    yield
                finally:
                    held = False

            def inventory(*_args):
                self.assertTrue(held)
                return references

            references = (generation_id,)
            with (
                patch.object(provider_deployment, "_resolve_provider_image", return_value=IMAGES["provider"]),
                patch.object(provider_deployment, "inspect_provider_identity_lock_volume", return_value="identity-lock"),
                patch.object(provider_deployment, "_held_provider_identity_lock", side_effect=identity_lock),
                patch.object(
                    provider_deployment,
                    "_inspect_project_containers",
                    return_value={},
                ),
                patch.object(
                    provider_deployment,
                    "_journal_binding",
                    return_value=(None, b"", plan.binding_projection),
                ),
                patch.object(
                    provider_deployment,
                    "_journal_generation_ids",
                    side_effect=inventory,
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

            references = ()
            remove = provider_deployment._remove_retained_generation

            def remove_while_locked(*args):
                self.assertTrue(held)
                return remove(*args)

            with (
                patch.object(provider_deployment, "_resolve_provider_image", return_value=IMAGES["provider"]),
                patch.object(provider_deployment, "inspect_provider_identity_lock_volume", return_value="identity-lock"),
                patch.object(provider_deployment, "_held_provider_identity_lock", side_effect=identity_lock),
                patch.object(provider_deployment, "_remove_retained_generation", side_effect=remove_while_locked),
                patch.object(
                    provider_deployment,
                    "_inspect_project_containers",
                    return_value={},
                ),
                patch.object(
                    provider_deployment,
                    "_journal_binding",
                    return_value=(None, b"", plan.binding_projection),
                ),
                patch.object(
                    provider_deployment,
                    "_journal_generation_ids",
                    side_effect=inventory,
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

    def test_runtime_config_removal_keeps_selected_and_refuses_mounted_inputs(self):
        from nmrpeak_provider.provider_config import runtime_config_id
        with render_repository() as repository:
            plan = test_plan(repository)
            materialize_deployment_plan(repository, "production", plan)
            parent = repository / "secrets/deployments/production/runtime-configs"
            files = dict(plan.runtime_files)
            files["provider.toml"] += b"\n"
            old_id = runtime_config_id(files)
            old = provider_deployment._publish_retained_tree(parent, old_id, files)
            selected = parent / plan.runtime_config_id.removeprefix("sha256:")
            with self.assertRaisesRegex(DeploymentOperationRejected, "identity does not match"):
                provider_deployment._remove_retained_runtime_config(old, "sha256:" + "f" * 64)
            self.assertTrue(old.is_dir())
            with (
                patch.object(provider_deployment, "_inspect_project_containers", return_value={}),
                patch.object(provider_deployment, "render_deployment_plan", return_value=plan),
                patch.object(provider_deployment, "_resolve_provider_image", return_value=IMAGES["provider"]),
                patch.object(provider_deployment, "inspect_provider_identity_lock_volume", return_value="lock"),
                patch.object(provider_deployment, "_held_provider_identity_lock", return_value=nullcontext()),
                patch.object(provider_deployment, "_require_snapshot_unmounted", side_effect=DeploymentOperationRejected("mounted")) as inventory,
            ):
                with self.assertRaisesRegex(DeploymentOperationRejected, "selected runtime"):
                    provider_deployment.remove_runtime_config(repository, "production", plan.runtime_config_id, plan.runtime_config_id)
                inventory.assert_not_called()
                with self.assertRaisesRegex(DeploymentOperationRejected, "mounted"):
                    provider_deployment.remove_runtime_config(repository, "production", old_id, old_id)
                self.assertTrue(old.is_dir())
                inventory.side_effect = None
                provider_deployment.remove_runtime_config(repository, "production", old_id, old_id)
            self.assertFalse(old.exists())
            self.assertTrue(selected.is_dir())

    def test_runtime_config_borrower_inventory_includes_stopped_containers(self):
        container_id = "a" * 64
        target = Path("/private/runtime/target")
        def command(_docker, arguments, **_kwargs):
            if arguments[0] == "ps":
                output = (container_id + "\n").encode()
            else:
                output = json.dumps([{"Id": container_id, "Mounts": [
                    {"Type": "bind", "Source": str(target / "input")},
                ]}]).encode()
            return subprocess.CompletedProcess(arguments, 0, output, b"")
        with patch.object(provider_deployment, "_docker_command", side_effect=command):
            with self.assertRaisesRegex(DeploymentOperationRejected, "still mounts"):
                provider_deployment._require_snapshot_unmounted(Path("/usr/bin/docker"), target)

    def test_runtime_config_borrower_inventory_checks_volume_backing_sources(self):
        target = Path("/private/runtime/target")
        def command(_docker, arguments, **_kwargs):
            if arguments[0] == "ps":
                output = b""
            elif arguments[:2] == ("volume", "ls"):
                output = b"retained-volume\n"
            else:
                output = json.dumps([{"Name": "retained-volume", "Driver": "local",
                                      "Options": {"o": "bind,ro", "device": str(target)}}]).encode()
            return subprocess.CompletedProcess(arguments, 0, output, b"")
        with patch.object(provider_deployment, "_docker_command", side_effect=command):
            with self.assertRaisesRegex(DeploymentOperationRejected, "volume still references"):
                provider_deployment._require_snapshot_unmounted(Path("/usr/bin/docker"), target)

    def test_runtime_config_borrower_inventory_checks_every_container_batch(self):
        identifiers = [f"{number:064x}" for number in range(1, 10)]
        def command(_docker, arguments, **_kwargs):
            if arguments[0] == "ps":
                output = ("\n".join(identifiers) + "\n").encode()
            elif arguments[0] == "inspect":
                output = json.dumps([{"Id": value, "Mounts": []} for value in arguments[1:]]).encode()
            else:
                output = b""
            return subprocess.CompletedProcess(arguments, 0, output, b"")
        with patch.object(provider_deployment, "_docker_command", side_effect=command) as docker:
            provider_deployment._require_snapshot_unmounted(Path("/usr/bin/docker"), Path("/private/runtime/target"))
        inspections = [call.args[1] for call in docker.call_args_list if call.args[1][0] == "inspect"]
        self.assertEqual([len(batch) - 1 for batch in inspections], [8, 1])

    def test_runtime_config_borrower_inventory_checks_every_volume_batch(self):
        names = {f"volume-{number}" for number in range(9)}
        def command(_docker, arguments, **_kwargs):
            output = json.dumps([{"Name": name, "Driver": "local", "Options": None}
                                 for name in arguments[2:]]).encode()
            return subprocess.CompletedProcess(arguments, 0, output, b"")
        with patch.object(provider_deployment, "_docker_command", side_effect=command) as docker:
            provider_deployment._require_volume_sources_unmounted(Path("/usr/bin/docker"), names, Path("/private/runtime/target"))
        self.assertEqual([len(call.args[1]) - 2 for call in docker.call_args_list], [8, 1])

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

    def test_journal_inspection_is_stopped_owned_and_restricted(self) -> None:
        output = canonical_json_bytes({
            "schema_id": "nmrpeak.journal_inspection.v1",
            "current_automation": "stopped", "observed_at": "2026-09-14T00:00:00Z",
            "stage_counts": {}, "records": [],
        }) + b"\n"
        with render_repository() as repository:
            materialize_deployment_plan(repository, "production", test_plan(repository))
            for running, attachments, failure in (
                (True, (), None), (False, ("foreign",), None),
                (False, (), DeploymentOperationRejected("module failed")),
                (False, (), None),
            ):
                with (
                    self.subTest(running=running, attachments=attachments, failure=failure),
                    patch.object(provider_deployment, "_inspect_project_containers",
                                 return_value={"provider": {"Id": "owned", "State": {"Running": running}}}),
                    patch.object(provider_deployment, "inspect_provider_journal_volume",
                                 return_value=("nmrpeak-production-journal-v1", attachments)),
                    patch.object(provider_deployment, "inspect_provider_identity_lock_volume",
                                 return_value="identity-lock"),
                    patch.object(provider_deployment, "_resolve_provider_image", return_value=IMAGES["provider"]),
                    patch.object(provider_deployment, "_held_provider_identity_lock", return_value=nullcontext()) as lock,
                    patch.object(provider_deployment, "_docker_command", side_effect=failure,
                                 return_value=subprocess.CompletedProcess((), 0, output, b"")) as command,
                ):
                    if running or attachments or failure:
                        with self.assertRaises(DeploymentOperationRejected) as caught:
                            provider_deployment.inspect_provider_journal(repository, "production")
                        if running:
                            self.assertIn("make provider/deployment/down DEPLOYMENT=production", str(caught.exception))
                        if running or attachments:
                            command.assert_not_called()
                        continue
                    self.assertEqual(provider_deployment.inspect_provider_journal(repository, "production"), output)
                    lock.assert_called_once()
                    args = command.call_args.args[1]
                    self.assertEqual(args[-3:], (IMAGES["provider"].image_id, "-m", "nmrpeak_provider.journal_inspect"))
                    self.assertEqual(args[args.index("--network") + 1], "none")
                    self.assertIn("--read-only", args)
                    self.assertEqual(args[args.index("--mount") + 1],
                                     "type=volume,src=nmrpeak-production-journal-v1,dst=/var/lib/nmrpeak-provider,readonly")
                    self.assertEqual(args.count("--mount"), 1)
                    for bad in ({"records": [None]}, {"body_base64": "private"}):
                        document = parse_canonical_json_bytes(output[:-1])
                        document.update(bad)
                        command.return_value = subprocess.CompletedProcess(
                            (), 0, canonical_json_bytes(document) + b"\n", b""
                        )
                        with self.assertRaisesRegex(DeploymentOperationRejected, "invalid output"):
                            provider_deployment.inspect_provider_journal(repository, "production")

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

    def test_identity_lock_cleanup_preserves_primary_and_secondary_causes(self) -> None:
        class Holder:
            stdin = BytesIO()
            stdout = BytesIO(b"READY\n")

            def poll(self):
                return None

        primary = OSError("Journal retirement write failed")
        cleanup = OSError("Supervisor removal failed; Bearer hidden")
        with TemporaryDirectory() as temporary, patch.object(
            subprocess, "Popen", return_value=Holder()), patch.object(
            provider_deployment.select, "select", return_value=([Holder.stdout], [], [])), patch.object(
            provider_deployment, "_stop_identity_lock_holder", side_effect=cleanup), self.assertRaises(DeploymentOperationRejected) as raised:
            with provider_deployment._run_held_provider_identity_lock(
                Path("/usr/bin/docker"), "lock-volume", "provider:nmrpeak",
                LocalImage("sha256:" + "1" * 64, "sha256:" + "2" * 64), Path(temporary) / "holder.cid"):
                raise primary
        self.assertIs(raised.exception.__cause__, primary)
        rendered = provider_deployment._render_deployment_failure("journal-retire", "production", raised.exception)
        self.assertIn("Journal retirement write failed", rendered)
        self.assertIn("Supervisor removal failed", rendered)
        self.assertNotIn("hidden", rendered)

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

    def test_journal_retirement_removes_only_after_empty_complete_inventory(self) -> None:
        from contextlib import contextmanager
        from nmrpeak_provider.provider_config import decode_provider_template
        with render_repository() as repository:
            plan = test_plan(repository)
            materialize_deployment_plan(repository, "production", plan)
            journal = "nmrpeak-production-journal-v1"
            image = IMAGES["provider"]
            policy = decode_provider_template((repository / "config/deployments/production/provider.toml").read_bytes())
            events: list[str] = []
            @contextmanager
            def held_route(*_args, **_kwargs):
                events.append("route-start")
                yield policy, plan.binding_projection, Path("/tmp/captured-api")
                events.append("route-cleanup")
            @contextmanager
            def held_identity(*_args, **_kwargs):
                events.append("identity-lock-start")
                yield
                events.append("identity-lock-release")
            def run_client(*_args, **_kwargs):
                events.append("inventory")
                return b"complete-empty\n"
            def remove(*args):
                events.append("delete")
                return journal
            with (
                patch.object(provider_deployment, "_inspect_project_containers", return_value={}),
                patch.object(provider_deployment, "_journal_binding", return_value=(policy, b"", plan.binding_projection)),
                patch.object(provider_deployment, "inspect_provider_journal_volume", return_value=(journal, ())),
                patch.object(provider_deployment, "inspect_provider_identity_lock_volume", return_value="nmrpeak-provider-lock-test"),
                patch.object(provider_deployment, "_resolve_provider_image", return_value=image),
                patch.object(provider_deployment, "_held_provider_identity_lock", side_effect=held_identity),
                patch.object(provider_deployment, "_held_maintenance_api_route", side_effect=held_route),
                patch.object(provider_deployment, "_run_journal_api_client", side_effect=run_client),
                patch.object(provider_deployment, "remove_provider_journal_volume", side_effect=remove) as remove_volume,
            ):
                self.assertEqual(retire_provider_journal(repository, "production", journal), journal)
            self.assertEqual(events, ["identity-lock-start", "route-start", "inventory", "route-cleanup", "delete", "identity-lock-release"])
            remove_volume.assert_called_once_with(Path("/usr/bin/docker"), "production", "provider:nmrpeak", plan.binding_projection["journal_authority_id"])



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
            "/config/deployments/\n/secrets/\n",
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
        self.external = TemporaryDirectory()
        external = Path(self.external.name).resolve()
        credential = external / "first.json"
        credential.write_bytes(credential_bytes(Ed25519PrivateKey.generate()))
        credential.chmod(0o600)
        provider_template = (ROOT / "config/provider.toml.example").read_bytes()
        provider_template = provider_template.replace(
            b"/absolute/path/to/provider-credential.json", str(credential).encode("utf-8")
        )
        provider_template = provider_template.replace(b'ca_file = "/absolute/path/to/api-ca.pem"\n', b"")
        provider_template = provider_template.replace(b"provider:example", b"provider:nmrpeak")
        (root / "config/deployments/production/provider.toml").write_bytes(provider_template)
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
        (root / ".gitignore").write_text("/secrets/\n", encoding="utf-8")
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
        endpoint_root = root / "secrets/deployments/production/openai-chat-completions.d"
        endpoint_root.mkdir(parents=True, mode=0o700)
        for directory in (root / "secrets", root / "secrets/deployments", endpoint_root.parent, endpoint_root):
            directory.chmod(0o700)
        endpoint = endpoint_root / "10-primary.toml"
        endpoint.write_text('id="primary"\nbase_url="https://example.test"\napi_key="test-secret"\nmodel="model"\n', encoding="utf-8")
        endpoint.chmod(0o600)
        self._projection_patch = patch.object(provider_deployment, "_contained_shared_projection", side_effect=self._project_shared)
        self._projection_patch.start()
        self._validate_patch = patch.object(provider_deployment, "_validate_captured_runtime", side_effect=self._validate_captured)
        self._validate_patch.start()
        return root

    def _project_shared(self, _docker, _image, provider, *, include_shared=True):
        from nmrpeak_provider.deployment_runtime import project_api_inputs
        from nmrpeak_provider.provider_config import decode_provider_template
        if not include_shared:
            return project_api_inputs(decode_provider_template(provider))
        with TemporaryDirectory() as temporary:
            provider_path = Path(temporary) / "provider.toml"
            provider_path.write_bytes(provider)
            return project_provider_inputs(provider_path)

    def _validate_captured(self, _docker, _image, files, *, arguments=("validate",)):
        import tomllib
        from nmrpeak_provider.deployment_runtime import api_facts
        if arguments == ("validate",):
            configured = tomllib.loads(files["provider.toml"].decode("utf-8"))
            return api_facts(configured["api"]) | {
                "frozen_generation_id": configured["frozen_generation_id"],
                "proxy_socket": configured["api_route"]["proxy_socket"],
                "outbox_socket": configured["api_route"]["outbox_socket"],
            }
        api = json.loads(files["api.json"])
        return api_facts(api) | {"proxy_socket": arguments[arguments.index("--proxy-socket") + 1]}

    def __exit__(self, *exc_info: object) -> None:
        self._validate_patch.stop()
        self._projection_patch.stop()
        self.external.cleanup()
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


def compose_for_environment(
    deployment: str,
    environment: dict[str, str],
    *,
    binding_projection: dict,
) -> dict[str, object]:
    document = compose_document(binding_projection)
    provider = document["services"]["provider"]
    shared = binding_projection["shared"]
    targets = {mount["target"] for mount in shared["mounts"]}
    provider["volumes"] = [mount for mount in provider["volumes"] if mount["target"] not in targets]
    provider.pop("group_add", None)
    for name in shared["volumes"]:
        document["volumes"].pop(name)
    document["name"] = f"nmrpeak-{deployment}"
    services = document["services"]
    services["provider"]["image"] = environment["PROVIDER_IMAGE_REF"]
    services["hf-runner"]["image"] = environment["HF_RUNNER_IMAGE_REF"]
    services["chf-runner"]["image"] = environment["CHF_RUNNER_IMAGE_REF"]
    services["hf-runner"]["command"] = ["--checkpoint-ref", environment["HF_CHECKPOINT_REF"],
                                          "--image-input-id", environment["HF_RUNNER_IMAGE_INPUT_ID"]]
    services["chf-runner"]["command"] = ["--checkpoint-ref", environment["CHF_CHECKPOINT_REF"],
                                           "--image-input-id", environment["CHF_RUNNER_IMAGE_INPUT_ID"]]
    runtime = next(mount for mount in services["provider"]["volumes"]
                   if mount["target"] == "/run/config/nmrpeak-provider")
    runtime["source"] = environment["PROVIDER_RUNTIME_DIRECTORY"]
    frozen = next(mount for mount in services["provider"]["volumes"]
                  if mount["target"] == "/run/nmrpeak-provider/frozen")
    frozen["source"] = environment["FROZEN_GENERATION_PATH"]
    return provider_deployment._compose_binding_route(document, binding_projection)


def test_plan(repository: Path) -> DeploymentPlan:
    with (
        patch.object(provider_deployment, "_image_input_ids", return_value=INPUTS),
        patch.object(provider_deployment, "_local_images", return_value=IMAGES),
        patch.object(provider_deployment, "_render_compose",
                     side_effect=lambda _, deployment, environment, __, *, binding_projection:
                         compose_for_environment(deployment, environment, binding_projection=binding_projection)),
    ):
        return render_deployment_plan(repository, "production")


def ready_services(plan: DeploymentPlan) -> dict[str, dict[str, object]]:
    ready = {}
    for service in plan.compose["services"]:
        if service == "api-init":
            state = {"Status": "exited", "ExitCode": 0}
        else:
            state = {"Status": "running"}
            if service in {"provider", "api"}:
                state["Health"] = {"Status": "healthy"}
        ready[service] = {"Image": plan.compose["services"][service]["image"], "State": state}
    return ready


if __name__ == "__main__":
    unittest.main()
