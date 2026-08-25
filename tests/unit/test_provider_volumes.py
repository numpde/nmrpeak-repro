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
    inspect_provider_identity_lock_volume,
    inspect_provider_journal_volume,
    remove_provider_identity_lock,
    remove_provider_journal_volume,
)

AUTHORITY_ID = "sha256:" + "d" * 64


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
                AUTHORITY_ID,
            )
            second = ensure_provider_state_volumes(
                Path("/usr/bin/docker"),
                Path(temporary),
                "production",
                "provider:nmrpeak",
                AUTHORITY_ID,
            )
            engine.attachments[first.journal] = ["b" * 64]
            self.assertEqual(
                inspect_provider_journal_volume(
                    Path("/usr/bin/docker"),
                    "production",
                    "provider:nmrpeak",
                    AUTHORITY_ID,
                ),
                (first.journal, ("b" * 64,)),
            )
            self.assertEqual(
                inspect_provider_identity_lock_volume(
                    Path("/usr/bin/docker"),
                    "provider:nmrpeak",
                ),
                first.identity_lock,
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
                AUTHORITY_ID,
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
                    AUTHORITY_ID,
                )
            after = len([command for command in engine.commands if command[0] == "run"])
        self.assertEqual(after, before)

    def test_journal_authority_drift_is_rejected(self) -> None:
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
                AUTHORITY_ID,
            )
            with self.assertRaisesRegex(
                ProviderVolumeOperationRejected,
                "ownership has drifted",
            ):
                inspect_provider_journal_volume(
                    Path("/usr/bin/docker"),
                    "production",
                    "provider:nmrpeak",
                    "sha256:" + "e" * 64,
                )
            self.assertIn(admitted.journal, engine.volumes)

    def test_identity_lock_removal_requires_confirmation_and_no_residue(self) -> None:
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
                AUTHORITY_ID,
            )
            with self.assertRaisesRegex(
                ProviderVolumeOperationRejected,
                "full volume name",
            ):
                remove_provider_identity_lock(
                    Path("/usr/bin/docker"),
                    Path(temporary),
                    "provider:nmrpeak",
                    "wrong",
                )
            engine.attachments[admitted.identity_lock] = ["a" * 64]
            with self.assertRaisesRegex(
                ProviderVolumeOperationRejected,
                "attachments",
            ):
                remove_provider_identity_lock(
                    Path("/usr/bin/docker"),
                    Path(temporary),
                    "provider:nmrpeak",
                    admitted.identity_lock,
                )
            engine.attachments[admitted.identity_lock] = []
            self.assertEqual(
                remove_provider_identity_lock(
                    Path("/usr/bin/docker"),
                    Path(temporary),
                    "provider:nmrpeak",
                    admitted.identity_lock,
                ),
                admitted.identity_lock,
            )

        self.assertNotIn(admitted.identity_lock, engine.volumes)
        self.assertIn(admitted.journal, engine.volumes)

    def test_journal_removal_reproves_no_late_attachment(self) -> None:
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
                AUTHORITY_ID,
            )
            engine.attachments[admitted.journal] = ["c" * 64]
            with self.assertRaisesRegex(
                ProviderVolumeOperationRejected,
                "attachments",
            ):
                remove_provider_journal_volume(
                    Path("/usr/bin/docker"),
                    "production",
                    "provider:nmrpeak",
                    AUTHORITY_ID,
                )
            self.assertIn(admitted.journal, engine.volumes)

            engine.attachments[admitted.journal] = []
            self.assertEqual(
                remove_provider_journal_volume(
                    Path("/usr/bin/docker"),
                    "production",
                    "provider:nmrpeak",
                    AUTHORITY_ID,
                ),
                admitted.journal,
            )
            self.assertNotIn(admitted.journal, engine.volumes)


class FakeEngine:
    def __init__(self) -> None:
        self.volumes: dict[str, dict[str, object]] = {}
        self.attachments: dict[str, list[str]] = {}
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
        if arguments[:2] == ("volume", "rm"):
            name = arguments[2]
            del self.volumes[name]
            return result(name.encode() + b"\n")
        if arguments[:2] == ("ps", "--all"):
            name = arguments[arguments.index("--filter") + 1].removeprefix("volume=")
            attachments = self.attachments.get(name, [])
            return result(
                ("\n".join(attachments) + ("\n" if attachments else "")).encode()
            )
        if arguments[0] == "run":
            return result(b"")
        raise AssertionError(arguments)


def result(stdout: bytes):
    from subprocess import CompletedProcess

    return CompletedProcess((), 0, stdout, b"")


if __name__ == "__main__":
    unittest.main()
