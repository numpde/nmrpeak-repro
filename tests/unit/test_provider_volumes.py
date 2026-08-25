"""Prove provider volume creation and ownership remain exact and idempotent."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import deployment.provider_volumes as provider_volumes
from deployment.provider_volumes import (
    ProviderVolumeOperationRejected,
    ensure_provider_state_volumes,
)


class ProviderVolumesTests(unittest.TestCase):
    def test_exact_volumes_are_created_initialized_and_reused(self) -> None:
        engine = FakeEngine()
        with TemporaryDirectory() as temporary, patch.object(
            provider_volumes,
            "_committed_helper_path",
            return_value=Path(temporary) / "provider_volume.py",
        ), patch.object(provider_volumes, "_docker", side_effect=engine):
            first = ensure_provider_state_volumes(
                Path("/usr/bin/docker"),
                Path(temporary),
                "production",
                "provider:nmrpeak",
            )
            second = ensure_provider_state_volumes(
                Path("/usr/bin/docker"),
                Path(temporary),
                "production",
                "provider:nmrpeak",
            )

        self.assertEqual(first, second)
        self.assertEqual(engine.created, [first.identity_lock, first.journal])
        helper_commands = [command for command in engine.commands if command[0] == "run"]
        self.assertEqual(len(helper_commands), 4)
        self.assertNotIn("--cap-add", helper_commands[0])
        self.assertEqual(
            helper_commands[1][
                helper_commands[1].index("--cap-add") :
                helper_commands[1].index("--cap-add") + 2
            ],
            ("--cap-add", "CHOWN"),
        )

    def test_foreign_labels_are_rejected_before_volume_initialization(self) -> None:
        engine = FakeEngine()
        with TemporaryDirectory() as temporary, patch.object(
            provider_volumes,
            "_committed_helper_path",
            return_value=Path(temporary) / "provider_volume.py",
        ), patch.object(provider_volumes, "_docker", side_effect=engine):
            admitted = ensure_provider_state_volumes(
                Path("/usr/bin/docker"),
                Path(temporary),
                "production",
                "provider:nmrpeak",
            )
            engine.volumes[admitted.identity_lock]["Labels"]["foreign"] = "true"
            before = len([command for command in engine.commands if command[0] == "run"])
            with self.assertRaisesRegex(
                ProviderVolumeOperationRejected,
                "ownership has drifted",
            ):
                ensure_provider_state_volumes(
                    Path("/usr/bin/docker"),
                    Path(temporary),
                    "production",
                    "provider:nmrpeak",
                )
            after = len([command for command in engine.commands if command[0] == "run"])
        self.assertEqual(after, before)


class FakeEngine:
    def __init__(self) -> None:
        self.volumes: dict[str, dict[str, object]] = {}
        self.created: list[str] = []
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, _docker: Path, *arguments: str):
        self.commands.append(arguments)
        if arguments[:2] == ("volume", "ls"):
            name = arguments[arguments.index("--filter") + 1].removeprefix("name=^").removesuffix("$")
            records = b"" if name not in self.volumes else json.dumps({"Name": name}).encode() + b"\n"
            return result(records)
        if arguments[:2] == ("volume", "create"):
            name = arguments[-1]
            labels = {
                arguments[index + 1].split("=", 1)[0]: arguments[index + 1].split("=", 1)[1]
                for index, value in enumerate(arguments)
                if value == "--label"
            }
            self.volumes[name] = {"Name": name, "Driver": "local", "Labels": labels}
            self.created.append(name)
            return result(name.encode() + b"\n")
        if arguments[:2] == ("volume", "inspect"):
            return result(json.dumps([self.volumes[arguments[2]]]).encode())
        if arguments[0] == "run":
            return result(b"")
        raise AssertionError(arguments)


def result(stdout: bytes):
    from subprocess import CompletedProcess

    return CompletedProcess((), 0, stdout, b"")


if __name__ == "__main__":
    unittest.main()
