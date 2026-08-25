"""Prove contract projection reads immutable Git release objects and writes narrowly."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import repository_checks.nmr_api_projection as projection
from repository_checks.nmr_api_projection import (
    ProjectionRejected,
    check_projection,
    write_projection,
)
from nmrpeak_provider.provider_http_contract import PROVIDER_HTTP_RELEASE_REF


ROOT = Path(__file__).parents[2]
RELEASE = ROOT / "contracts/upstream/nmr_api_v1"


class NmrApiProjectionTests(unittest.TestCase):
    def test_projection_uses_committed_release_objects_not_worktree_bytes(self) -> None:
        with projection_fixture() as (repository, upstream):
            check_projection(repository, upstream, PROVIDER_HTTP_RELEASE_REF)
            manifest = upstream_release(upstream) / "manifest.json"
            manifest.write_bytes(b"working tree drift\n")

            check_projection(repository, upstream, PROVIDER_HTTP_RELEASE_REF)

    def test_check_rejects_destination_drift_and_an_unconsumed_release(self) -> None:
        with projection_fixture() as (repository, upstream):
            destination = repository / "contracts/upstream/nmr_api_v1/manifest.json"
            destination.write_bytes(b"drift\n")
            with self.assertRaisesRegex(ProjectionRejected, "differs"):
                check_projection(repository, upstream, PROVIDER_HTTP_RELEASE_REF)
            with self.assertRaisesRegex(ProjectionRejected, "consumed"):
                check_projection(repository, upstream, "sha256:" + "0" * 64)

    def test_write_repairs_only_the_fixed_projection_and_is_idempotent(self) -> None:
        with projection_fixture() as (repository, upstream):
            destination = repository / "contracts/upstream/nmr_api_v1"
            neighbor = destination.parent / "neighbor.txt"
            neighbor.write_text("preserve\n", encoding="utf-8")
            (destination / "manifest.json").write_bytes(b"drift\n")

            write_projection(repository, upstream, PROVIDER_HTTP_RELEASE_REF)
            first = snapshot(destination)
            write_projection(repository, upstream, PROVIDER_HTTP_RELEASE_REF)

            self.assertEqual(snapshot(destination), first)
            self.assertEqual(first, snapshot(RELEASE))
            self.assertEqual(neighbor.read_text(encoding="utf-8"), "preserve\n")

    def test_failed_publication_restores_the_previous_projection(self) -> None:
        with projection_fixture() as (repository, upstream):
            destination = repository / "contracts/upstream/nmr_api_v1"
            drift = b"drift retained after rollback\n"
            (destination / "manifest.json").write_bytes(drift)
            rename = projection.os.rename

            def fail_stage_publication(source: Path, target: Path) -> None:
                if Path(source).name.endswith(".staging") and Path(target) == destination:
                    raise OSError("injected publication failure")
                rename(source, target)

            with patch.object(
                projection.os,
                "rename",
                side_effect=fail_stage_publication,
            ), self.assertRaises(OSError):
                write_projection(repository, upstream, PROVIDER_HTTP_RELEASE_REF)

            self.assertEqual((destination / "manifest.json").read_bytes(), drift)
            self.assertEqual(
                [path.name for path in destination.parent.iterdir()],
                ["nmr_api_v1"],
            )


class projection_fixture:
    def __init__(self) -> None:
        self.temporary = TemporaryDirectory()

    def __enter__(self) -> tuple[Path, Path]:
        root = Path(self.temporary.name).resolve()
        repository = root / "deploy"
        upstream = root / "nmr-api-v1"
        destination = repository / "contracts/upstream/nmr_api_v1"
        destination.parent.mkdir(parents=True)
        shutil.copytree(RELEASE, destination)
        release = upstream_release(upstream)
        release.parent.mkdir(parents=True)
        shutil.copytree(RELEASE, release)
        subprocess.run(("git", "init", "-q", str(upstream)), check=True)
        subprocess.run(("git", "-C", str(upstream), "add", "."), check=True)
        subprocess.run(
            (
                "git",
                "-C",
                str(upstream),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "commit",
                "-qm",
                "Add provider contract release",
            ),
            check=True,
        )
        return repository, upstream

    def __exit__(self, *exc_info: object) -> None:
        self.temporary.cleanup()


def upstream_release(upstream: Path) -> Path:
    digest = PROVIDER_HTTP_RELEASE_REF.removeprefix("sha256:")
    return upstream / f"nmr_api/provider/contract_releases/v1/{digest}"


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


if __name__ == "__main__":
    unittest.main()
