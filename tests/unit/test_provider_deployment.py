"""Prove initialization publishes only a new literal deployment config."""

from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

import deployment.provider_deployment as provider_deployment
from deployment.provider_deployment import (
    DeploymentOperationRejected,
    initialize_deployment,
)


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


if __name__ == "__main__":
    unittest.main()
