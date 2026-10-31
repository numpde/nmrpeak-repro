"""Prove operator supplied Make values remain data at shell boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import pty
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest


REPOSITORY_ROOT = Path(__file__).parents[2]


class MakeBoundaryTests(unittest.TestCase):
    def _docker_context_environment(
        self,
        directory: str,
        contexts: dict[str, str] | None = None,
    ) -> dict[str, str]:
        root = Path(directory)
        docker = root / "docker"
        contexts = contexts or {"default": "unix:///tmp/nmrpeak-test.sock"}
        base_key = subprocess.run(
            ("bash", str(REPOSITORY_ROOT / "scripts/test-image-key.sh"), str(REPOSITORY_ROOT)),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        log = root / "docker-calls"
        docker.write_text(
            f'''#!{sys.executable}
import os
import sys
CONTEXTS = {contexts!r}
args = sys.argv[1:]
if args == ["context", "ls", "--format", "{{{{.Name}}}}"]:
    print("\\n".join(CONTEXTS))
elif len(args) >= 4 and args[0] == "--context" and args[2:4] == ["context", "inspect"]:
    with open(os.environ["NMRPEAK_TEST_DOCKER_CALL_LOG"], "a", encoding="utf-8") as log:
        log.write("context inspect " + args[1] + "\\n")
    endpoint = CONTEXTS.get(args[1])
    if endpoint is None:
        raise SystemExit(90)
    print(endpoint)
elif len(args) >= 3 and args[0] == "--host":
    with open(os.environ["NMRPEAK_TEST_DOCKER_CALL_LOG"], "a", encoding="utf-8") as log:
        log.write(repr(args) + "\\n")
    operation = args[2:]
    if operation[:2] == ["image", "ls"]:
        print("sha256:" + "a" * 64)
    elif operation[:2] == ["image", "inspect"]:
        print({base_key!r})
    elif operation[:2] != ["compose", "--env-file"]:
        raise SystemExit(90)
else:
    raise SystemExit(90)
''',
            encoding="utf-8",
        )
        docker.chmod(0o755)
        return {
            **os.environ,
            "PATH": f"{directory}:{os.environ['PATH']}",
            "NMRPEAK_TEST_DOCKER_CALL_LOG": str(log),
        }

    def _assert_dry_run_keeps_values_out_of_shell(self, target: str, values: tuple[str, ...]) -> None:
        marker = "/tmp/nmrpeak-make-injection-marker"
        Path(marker).unlink(missing_ok=True)
        injected = f'payload"; touch {marker}; echo "'
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                ("make", "--dry-run", target, *(f"{name}={injected}" for name in values), "DOCKER_CONTEXT=default"),
                cwd=REPOSITORY_ROOT,
                env=self._docker_context_environment(temporary),
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(f"touch {marker}", result.stdout)
        self.assertFalse(Path(marker).exists())

    def test_persistent_job_confirmation_cannot_inject_shell_syntax(self) -> None:
        marker = "/tmp/nmrpeak-make-injection-marker"
        Path(marker).unlink(missing_ok=True)
        common = (
            "LIVE_API_ORIGIN=https://example.invalid",
            "LIVE_API_TOPOLOGY=topology",
            "LIVE_USER_CREDENTIAL=/tmp/credential",
            "LIVE_PROJECT_REF=project",
            "LIVE_PROVIDER_REF=provider",
            "LIVE_RUN_LABEL=run",
            "LIVE_STATE=/tmp/state",
            "LIVE_SOURCE_REVISION=" + "0" * 40,
            f'CONFIRM_PERSISTENT_JOBS=1"; touch {marker}; echo "',
        )
        for target, additional in (
            ("test/live/failure-propagation", ()),
            (
                "test/live/success-propagation",
                (
                    "LIVE_EXPECTED_HF_CHECKPOINT=hf",
                    "LIVE_EXPECTED_CHF_CHECKPOINT=chf",
                    "LIVE_EXPECTED_HF_IMAGE_INPUT_ID=" + "sha256:" + "1" * 64,
                    "LIVE_EXPECTED_CHF_IMAGE_INPUT_ID=" + "sha256:" + "2" * 64,
                ),
            ),
        ):
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as temporary:
                    result = subprocess.run(
                        ("make", "--dry-run", target, *common, *additional, "DOCKER_CONTEXT=default"),
                        cwd=REPOSITORY_ROOT,
                        env=self._docker_context_environment(temporary),
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(f"touch {marker}", result.stdout)
                self.assertFalse(Path(marker).exists())

    def test_operator_target_values_cannot_inject_shell_syntax(self) -> None:
        cases = (
            ("runner/lock/stage", ("TARGET",)),
            ("runner/lock/check", ("TARGET",)),
            ("runner/lock/apply", ("TARGET",)),
            ("runner/image/build", ("RUNNER", "TARGET")),
            ("provider/image/build", ("NETWORK_INTERFACE",)),
            ("benchmark/image/build", ("NETWORK_INTERFACE",)),
            ("checkpoint/import", ("RUNNER", "RELEASE", "ARCHIVE")),
            ("checkpoint/recover", ("VOLUME", "CONFIRM")),
        )
        for target, values in cases:
            with self.subTest(target=target):
                self._assert_dry_run_keeps_values_out_of_shell(target, values)

    def test_image_builder_rejects_an_unavailable_interface(self) -> None:
        result = subprocess.run(
            (str(REPOSITORY_ROOT / "scripts/test-image-base.sh"), str(REPOSITORY_ROOT), "not-an-interface"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not exist", result.stderr)

    def test_docker_make_target_requires_explicit_or_interactive_context(self) -> None:
        result = subprocess.run(
            ("make", "--dry-run", "provider/image/build"),
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "DOCKER_CONTEXT": "default"},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("from the environment or Makefile is implicit", result.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            environment = self._docker_context_environment(
                temporary,
                {"local-a": "unix:///tmp/local-a.sock"},
            )
            environment.pop("DOCKER_CONTEXT", None)
            result = subprocess.run(
                ("make", "--dry-run", "provider/image/build"),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("no interactive terminal is available", result.stderr)

    def test_make_shell_does_not_trust_ambient_docker_selection_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = self._docker_context_environment(
                temporary,
                {
                    "default": "unix:///tmp/nmrpeak-test.sock",
                    "local-a": "unix:///tmp/local-a.sock",
                },
            )
            for name in (
                "DOCKER_CONTEXT",
                "NMRPEAK_DOCKER_HOST",
                "NMRPEAK_DOCKER_CONTEXT_INPUT",
                "NMRPEAK_DOCKER_CONTEXT_INPUT_ORIGIN",
                "NMRPEAK_DOCKER_ENDPOINT_INPUT",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "MAKELEVEL": "1",
                    "NMRPEAK_DOCKER_HOST": "unix:///tmp/poisoned.sock",
                    "NMRPEAK_DOCKER_CONTEXT_INPUT": "default",
                    "NMRPEAK_DOCKER_CONTEXT_INPUT_ORIGIN": "command line",
                    "NMRPEAK_DOCKER_ENDPOINT_INPUT": "unix:///tmp/poisoned-endpoint.sock",
                }
            )

            result = subprocess.run(
                ("make", "--dry-run", "check/test-image"),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("no interactive terminal is available", result.stderr)

            result = subprocess.run(
                ("make", "check/test-image", "DOCKER_CONTEXT=local-a"),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = Path(temporary, "docker-calls").read_text(encoding="utf-8")
            self.assertIn("--host', 'unix:///tmp/local-a.sock", calls)
            self.assertNotIn("poisoned", calls)

    def test_context_name_is_data_at_the_make_shell_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "unexpected"
            result = subprocess.run(
                (
                    "make",
                    "--dry-run",
                    "provider/image/build",
                    f'DOCKER_CONTEXT=x"; touch {marker}; #',
                ),
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("valid Docker context name", result.stderr)
            self.assertFalse(marker.exists())

    def test_non_docker_make_target_does_not_require_context(self) -> None:
        result = subprocess.run(
            ("make", "--dry-run", "check/source"),
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        result = subprocess.run(
            ("make", "help"),
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unclassified_docker_goal_fails_closed(self) -> None:
        result = subprocess.run(
            (
                "make",
                "--eval",
                "unclassified-docker-goal:; @$(DOCKER) ps",
                "unclassified-docker-goal",
                "DOCKER=docker",
            ),
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "NMRPEAK_DOCKER_HOST": "unix:///tmp/ambient.sock"},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker command used without context selection", result.stderr)

    def test_maintained_docker_owners_do_not_restore_a_default_context(self) -> None:
        owners = (
            REPOSITORY_ROOT / "Makefile",
            *sorted((REPOSITORY_ROOT / "scripts").glob("*.sh")),
            *sorted((REPOSITORY_ROOT / "benchmark_tools").glob("*.py")),
            *sorted((REPOSITORY_ROOT / "repository_checks").glob("*.py")),
            *sorted((REPOSITORY_ROOT / "deployment").glob("*.py")),
        )
        fallback = re.compile(
            r"--context(?:=|\s+)[\"']?[A-Za-z0-9_.-]+[\"']?(?=\s|$)|"
            r"[\"']--context[\"']\s*,\s*[\"'][A-Za-z0-9_.-]+[\"']|"
            r"context\s+inspect\s+[\"']?[A-Za-z0-9_.-]+[\"']?(?=\s|$)|"
            r"[\"']context[\"']\s*,\s*[\"']inspect[\"']\s*,\s*[\"'][A-Za-z0-9_.-]+[\"']|"
            r"^[ \t]*(?:override\s+|export\s+)*DOCKER_CONTEXT\s*[:?+]?=\s*[\"']?[A-Za-z0-9_.-]+[\"']?(?=\s|$)|"
            r"[\"']DOCKER_CONTEXT[\"']\s*:\s*[\"'][A-Za-z0-9_.-]+[\"']|"
            r"unix:///(?!\[)[^\s\"'$]+|"
            r"(?<![\w])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.sock",
            re.MULTILINE,
        )
        forbidden_fallbacks = (
            "DOCKER_CONTEXT := production",
            "export DOCKER_CONTEXT=production",
            "docker --context production context ls",
            "[\"--context\", \"production\"]",
            "docker context inspect production",
            "[\"context\", \"inspect\", \"production\"]",
            "{\"DOCKER_CONTEXT\": \"production\"}",
            "endpoint = 'unix:///run/user/1000/docker.sock'",
            "docker --host unix:///tmp/engine.sock info",
            "docker --host /run/user/1000/docker.sock info",
        )
        for source in forbidden_fallbacks:
            with self.subTest(source=source):
                self.assertIsNotNone(fallback.search(source))

        allowed_selected_values = (
            "make test DOCKER_CONTEXT=rootless",
            'docker --context "$context" context inspect',
            'docker --host "$NMRPEAK_DOCKER_HOST" info',
            '[[ "$endpoint" =~ ^unix:///[^[:space:]]+$ ]]',
        )
        for source in allowed_selected_values:
            with self.subTest(source=source):
                self.assertIsNone(fallback.search(source))

        for path in owners:
            with self.subTest(path=path.relative_to(REPOSITORY_ROOT)):
                self.assertIsNone(fallback.search(path.read_text(encoding="utf-8")))

        makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
        for recipe in (line for line in makefile.splitlines() if line.startswith("\t")):
            if re.search(r"\bdocker(?:\s|$)", recipe):
                self.assertIn("$(DOCKER)", recipe, recipe)

    def test_make_pins_the_selected_local_context_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = self._docker_context_environment(
                temporary,
                {
                    "default": "unix:///tmp/nmrpeak-test.sock",
                    "local-a": "unix:///tmp/local-a.sock",
                    "remote-a": "tcp://docker.invalid:2376",
                },
            )
            docker_calls = Path(temporary) / "docker-calls"
            result = subprocess.run(
                ("make", "--dry-run", "check/test-image", "DOCKER_CONTEXT=local-a"),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--host "$NMRPEAK_DOCKER_HOST"', result.stdout)
            self.assertNotIn("--context", result.stdout)

            result = subprocess.run(
                ("make", "check/test-image", "DOCKER_CONTEXT=local-a"),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = docker_calls.read_text(encoding="utf-8")
            self.assertIn("--host', 'unix:///tmp/local-a.sock", calls)
            self.assertIn("compose", calls)

            docker_calls.write_text("", encoding="utf-8")
            result = subprocess.run(
                ("make", "--dry-run", "check/test-image", "test", "DOCKER_CONTEXT=local-a"),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                docker_calls.read_text(encoding="utf-8").splitlines().count("context inspect local-a"),
                1,
            )

            result = subprocess.run(
                ("make", "--dry-run", "check/test-image", "DOCKER_CONTEXT=remote-a"),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("must name an existing context that uses a local Unix socket", result.stderr)

    def test_explicit_default_named_context_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = self._docker_context_environment(temporary)
            result = subprocess.run(
                ("make", "check/test-image", "DOCKER_CONTEXT=default"),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = Path(temporary, "docker-calls").read_text(encoding="utf-8")
            self.assertIn("--host', 'unix:///tmp/nmrpeak-test.sock", calls)

    def test_interactive_menu_selects_a_listed_local_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = self._docker_context_environment(
                temporary,
                {
                    "local-a": "unix:///tmp/local-a.sock",
                    "remote-a": "tcp://docker.invalid:2376",
                },
            )
            docker_calls = Path(temporary) / "docker-calls"
            child, terminal = pty.fork()
            if child == 0:
                environment.pop("DOCKER_CONTEXT", None)
                os.execvpe(
                    "make",
                    ("make", "check/test-image"),
                    environment,
                )

            output = bytearray()
            sent_choice = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                readable, _, _ = select.select((terminal,), (), (), 0.1)
                if readable:
                    try:
                        output.extend(os.read(terminal, 4096))
                    except OSError:
                        break
                    if b"Choice [1-1]:" in output and not sent_choice:
                        os.write(terminal, b"1\n")
                        sent_choice = True
                finished, status = os.waitpid(child, os.WNOHANG)
                if finished:
                    break
            else:
                os.kill(child, 9)
                os.waitpid(child, 0)
                self.fail("interactive Docker context selection timed out")
            os.close(terminal)
            decoded = output.decode("utf-8", errors="replace")
            self.assertTrue(sent_choice, decoded)
            self.assertTrue(os.WIFEXITED(status), decoded)
            self.assertEqual(os.WEXITSTATUS(status), 0, decoded)
            calls = docker_calls.read_text(encoding="utf-8")
            self.assertIn("--host', 'unix:///tmp/local-a.sock", calls)


if __name__ == "__main__":
    unittest.main()
