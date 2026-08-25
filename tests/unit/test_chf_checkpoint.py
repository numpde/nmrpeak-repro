"""Prove the CHF checkpoint operation against a stateful fake Docker CLI."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
import zipfile

from repository_checks.chf_checkpoint import (
    ChfCheckpointOperationRejected,
    checkpoint_volume_name,
    import_chf_checkpoint,
    recover_chf_checkpoint,
)
from repository_checks.chf_release import (
    ARCHIVE_MEMBER as CHF_ARCHIVE_MEMBER,
    candidate_release_bytes as chf_candidate_release_bytes,
)
from repository_checks.hf_checkpoint import import_hf_checkpoint, recover_hf_checkpoint
from repository_checks.hf_release import (
    ARCHIVE_MEMBER as HF_ARCHIVE_MEMBER,
    candidate_release_bytes as hf_candidate_release_bytes,
)


_SOURCE_REVISION = "1" * 40
_CHECKPOINT = b"checkpoint fixture bytes"


class ChfCheckpointOperationTests(unittest.TestCase):
    def test_import_creates_verifies_and_reuses_one_content_addressed_volume(self) -> None:
        with CheckpointOperationFixture() as fixture:
            first = fixture.import_checkpoint()
            second = fixture.import_checkpoint()
            self.assertEqual(first, second)
            volume = fixture.volume_directory(first.name)
            self.assertEqual((volume / "checkpoint.pt").read_bytes(), _CHECKPOINT)
            self.assertTrue((volume / ".nmrpeak-checkpoint.json").is_file())
            commands = fixture.commands()
            self.assertEqual(sum("volume create" in command for command in commands), 1)
            self.assertTrue(any("--network none" in command for command in commands))
            self.assertTrue(any(first.name in command for command in commands))

    def test_failed_population_removes_only_this_unattached_new_volume(self) -> None:
        with CheckpointOperationFixture() as fixture:
            fixture.fail_population.write_text("fail", encoding="ascii")
            with self.assertRaises(ChfCheckpointOperationRejected):
                fixture.import_checkpoint()
            self.assertEqual(list(fixture.volumes.iterdir()), [])

    def test_uncertain_create_output_reinspects_before_cleanup(self) -> None:
        with CheckpointOperationFixture() as fixture:
            fixture.bad_create_output.write_text("bad", encoding="ascii")
            with self.assertRaises(ChfCheckpointOperationRejected):
                fixture.import_checkpoint()
            self.assertEqual(list(fixture.volumes.iterdir()), [])

    def test_interrupted_attached_import_requires_confirmed_recovery(self) -> None:
        with CheckpointOperationFixture() as fixture:
            fixture.fail_population.write_text("fail", encoding="ascii")
            fixture.attached.write_text("attached", encoding="ascii")
            with self.assertRaises(ChfCheckpointOperationRejected) as raised:
                fixture.import_checkpoint()
            self.assertTrue(raised.exception.__notes__)
            volume_name = next(fixture.volumes.iterdir()).name
            with self.assertRaises(ChfCheckpointOperationRejected):
                fixture.recover(volume_name, "wrong")
            fixture.attached.unlink()
            fixture.recover(volume_name, volume_name)
            self.assertFalse(fixture.volume_directory(volume_name).exists())

    def test_recovery_rejects_admitted_foreign_and_renamed_state(self) -> None:
        with CheckpointOperationFixture() as fixture:
            admitted = fixture.import_checkpoint()
            with self.assertRaises(ChfCheckpointOperationRejected):
                fixture.recover(admitted.name, admitted.name)

            fixture.remove_marker(admitted.name)
            fixture.change_label(admitted.name, "foreign", "value")
            with self.assertRaises(ChfCheckpointOperationRejected):
                fixture.recover(admitted.name, admitted.name)

    def test_archive_rejection_happens_before_any_engine_command(self) -> None:
        with CheckpointOperationFixture() as fixture:
            malformed = fixture.root / "malformed.zip"
            malformed.write_bytes(b"PK\x03\x04incomplete")
            with self.assertRaises(ChfCheckpointOperationRejected):
                fixture.import_checkpoint(archive=malformed)
            self.assertEqual(fixture.commands(), [])

    def test_importer_requires_clean_tracked_helper_bytes(self) -> None:
        with CheckpointOperationFixture() as fixture:
            worker = fixture.repository / "docker/checkpoint_volume.py"
            worker.write_bytes(worker.read_bytes() + b"\n# drift\n")
            with self.assertRaises(ChfCheckpointOperationRejected):
                fixture.import_checkpoint()
            self.assertFalse(any("volume create" in line for line in fixture.commands()))

    def test_volume_name_requires_the_complete_checkpoint_digest(self) -> None:
        digest = "sha256:" + "a" * 64
        self.assertEqual(
            checkpoint_volume_name(digest),
            "nmrpeak-chf-checkpoint-" + "a" * 64,
        )
        for invalid in ("a" * 64, "sha256:" + "A" * 64, "sha256:short"):
            with self.subTest(invalid=invalid), self.assertRaises(
                ChfCheckpointOperationRejected
            ):
                checkpoint_volume_name(invalid)


class CheckpointOperationFixture:
    def __init__(self, lane: str = "chf") -> None:
        if lane not in {"chf", "hf"}:
            raise ValueError("checkpoint fixture lane must be chf or hf")
        self.lane = lane

    def __enter__(self) -> CheckpointOperationFixture:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        source_repository = Path(__file__).resolve().parents[2]
        for relative_path in (
            "docker/checkpoint_volume.py",
        ):
            destination = self.repository / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_repository / relative_path, destination)
        source = self.repository / "families/nmrpeak/source-closure.paths"
        source.parent.mkdir(parents=True)
        source.write_text(
            f"source_revision {_SOURCE_REVISION}\nroot LICENSE\n",
            encoding="ascii",
        )
        self.archive = self.root / "weights.zip"
        archive_member = (
            CHF_ARCHIVE_MEMBER if self.lane == "chf" else HF_ARCHIVE_MEMBER
        )
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr(archive_member, _CHECKPOINT)
        candidate = (
            chf_candidate_release_bytes
            if self.lane == "chf"
            else hf_candidate_release_bytes
        )
        self.release_name = f"{self.lane}-test-v1"
        release = candidate(
            self.archive,
            self.release_name,
            source_revision=_SOURCE_REVISION,
        )
        releases = self.repository / f"models/nmrpeak_{self.lane}_v1/releases"
        releases.mkdir(parents=True)
        (releases / f"{self.release_name}.json").write_bytes(release)
        subprocess.run(
            ("/usr/bin/git", "init", "-q", str(self.repository)),
            check=True,
        )
        subprocess.run(
            ("/usr/bin/git", "-C", str(self.repository), "add", "."),
            check=True,
        )
        subprocess.run(
            (
                "/usr/bin/git",
                "-C",
                str(self.repository),
                "-c",
                "user.name=Checkpoint test",
                "-c",
                "user.email=checkpoint@example.invalid",
                "commit",
                "-qm",
                "Fixture",
            ),
            check=True,
        )
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.state = self.root / "docker-state"
        self.state.mkdir()
        self.volumes = self.state / "volumes"
        self.volumes.mkdir()
        self.metadata = self.state / "metadata"
        self.metadata.mkdir()
        self.commands_path = self.state / "commands"
        self.fail_population = self.state / "fail-populate"
        self.bad_create_output = self.state / "bad-create-output"
        self.attached = self.state / "attached"
        self.docker = self.root / "docker"
        self.docker.write_text(fake_docker_source(self.state), encoding="utf-8")
        self.docker.chmod(0o755)
        return self

    def __exit__(self, *_error: object) -> None:
        self.temporary.cleanup()

    def import_checkpoint(self, *, archive: Path | None = None):
        importer = import_chf_checkpoint if self.lane == "chf" else import_hf_checkpoint
        return importer(
            self.repository,
            archive or self.archive,
            self.release_name,
            docker_binary=self.docker,
            runtime_directory=self.runtime,
        )

    def recover(self, volume: str, confirmation: str) -> None:
        recovery = recover_chf_checkpoint if self.lane == "chf" else recover_hf_checkpoint
        recovery(
            volume,
            confirmation,
            repository_root=self.repository,
            docker_binary=self.docker,
            runtime_directory=self.runtime,
        )

    def volume_directory(self, name: str) -> Path:
        return self.volumes / name

    def commands(self) -> list[str]:
        if not self.commands_path.exists():
            return []
        return self.commands_path.read_text(encoding="utf-8").splitlines()

    def remove_marker(self, name: str) -> None:
        marker = self.volume_directory(name) / ".nmrpeak-checkpoint.json"
        marker.chmod(0o644)
        marker.unlink()

    def change_label(self, name: str, key: str, value: str) -> None:
        metadata = self.metadata / f"{name}.json"
        document = json.loads(metadata.read_text(encoding="utf-8"))
        document["Labels"][key] = value
        metadata.write_text(json.dumps(document), encoding="utf-8")


def fake_docker_source(state: Path) -> str:
    worker = Path(__file__).resolve().parents[2] / "docker/checkpoint_volume.py"
    return f'''#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import shutil
import sys

STATE = Path({str(state)!r})
VOLUMES = STATE / "volumes"
METADATA = STATE / "metadata"
args = sys.argv[1:]
if args[:2] != ["--context", "default"]:
    raise SystemExit(90)
args = args[2:]
with (STATE / "commands").open("a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")

if args[:2] == ["volume", "ls"]:
    for directory in sorted(VOLUMES.iterdir()):
        print(json.dumps({{"Name": directory.name}}))
elif args[:2] == ["volume", "create"]:
    name = args[-1]
    labels = {{}}
    for index, value in enumerate(args):
        if value == "--label":
            key, label_value = args[index + 1].split("=", 1)
            labels[key] = label_value
    directory = VOLUMES / name
    directory.mkdir()
    (METADATA / f"{{name}}.json").write_text(
        json.dumps({{"Name": name, "Driver": "local", "Labels": labels}}),
        encoding="utf-8",
    )
    print("wrong" if (STATE / "bad-create-output").exists() else name)
elif args[:2] == ["volume", "inspect"]:
    metadata = METADATA / f"{{args[2]}}.json"
    print("[" + metadata.read_text(encoding="utf-8") + "]")
elif args[:2] == ["volume", "rm"]:
    name = args[2]
    shutil.rmtree(VOLUMES / name)
    (METADATA / f"{{name}}.json").unlink()
    print(name)
elif args and args[0] == "ps":
    if (STATE / "attached").exists():
        print(json.dumps({{"ID": "container-id"}}))
elif args and args[0] == "run":
    mount = args[args.index("--mount") + 1]
    volume_name = mount.split("src=", 1)[1].split(",", 1)[0]
    image_index = next(
        index for index, value in enumerate(args)
        if value.startswith("docker.io/library/python:3.12.12-slim-bookworm@sha256:")
    )
    if args[image_index + 1:image_index + 3] != [
        "python", "/tool/checkpoint_volume.py"
    ]:
        raise SystemExit(92)
    helper_args = args[image_index + 3:]
    if helper_args[0] == "populate":
        if "--interactive" not in args[:image_index]:
            raise SystemExit(93)
    elif "--interactive" in args[:image_index]:
        raise SystemExit(94)
    if helper_args[0] == "populate" and (STATE / "fail-populate").exists():
        sys.stdin.buffer.read()
        raise SystemExit(2)
    spec = importlib.util.spec_from_file_location("volume_worker", {str(worker)!r})
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    volume = VOLUMES / volume_name
    module.VOLUME_PATH = volume
    module.CHECKPOINT_PATH = volume / "checkpoint.pt"
    module.MARKER_PATH = volume / ".nmrpeak-checkpoint.json"
    raise SystemExit(module.main(helper_args))
else:
    raise SystemExit(91)
'''


if __name__ == "__main__":
    unittest.main()
