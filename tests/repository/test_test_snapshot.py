from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import repository_checks.test_snapshot as snapshot_module
from repository_checks.test_snapshot import SnapshotRejected, materialize


class TestSnapshotTests(unittest.TestCase):
    def test_snapshot_rejects_a_head_change_after_sensitive_tree_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Snapshot Test")
            self._git(repository, "config", "user.email", "snapshot@example.invalid")
            (repository / "public.txt").write_text("public\n", encoding="utf-8")
            self._git(repository, "add", "public.txt")
            self._git(repository, "commit", "--quiet", "-m", "safe")
            safe_revision = self._git(repository, "rev-parse", "HEAD").strip()
            (repository / ".aws").mkdir()
            (repository / ".aws" / "credentials").write_text("private\n", encoding="utf-8")
            self._git(repository, "add", ".aws/credentials")
            self._git(repository, "commit", "--quiet", "-m", "unsafe")
            unsafe_revision = self._git(repository, "rev-parse", "HEAD").strip()
            self._git(repository, "reset", "--quiet", "--hard", safe_revision)
            original = snapshot_module._reject_tracked_sensitive_roots

            def advance_after_validation(source: Path, label: str, revision: str) -> None:
                original(source, label, revision)
                self._git(source, "reset", "--quiet", "--hard", unsafe_revision)

            with (
                mock.patch.object(
                    snapshot_module,
                    "_reject_tracked_sensitive_roots",
                    side_effect=advance_after_validation,
                ),
                self.assertRaisesRegex(SnapshotRejected, "Git revision changed"),
            ):
                materialize(repository, root / "snapshot")

    def test_snapshot_reports_a_tracked_non_utf8_path_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Snapshot Test")
            self._git(repository, "config", "user.email", "snapshot@example.invalid")
            raw_name = b"bad-\xff"
            raw_repository = os.fsencode(repository)
            descriptor = os.open(raw_repository + b"/" + raw_name, os.O_WRONLY | os.O_CREAT, 0o600)
            os.write(descriptor, b"fixture\n")
            os.close(descriptor)
            subprocess.run((b"git", b"-C", raw_repository, b"add", raw_name), check=True)
            self._git(repository, "commit", "--quiet", "-m", "fixture")

            with self.assertRaisesRegex(SnapshotRejected, "non-UTF-8 path"):
                materialize(repository, root / "snapshot")

    def test_snapshot_rejects_tracked_sensitive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Snapshot Test")
            self._git(repository, "config", "user.email", "snapshot@example.invalid")
            (repository / ".aws").mkdir()
            (repository / ".aws" / "credentials").write_text("private\n", encoding="utf-8")
            self._git(repository, "add", ".aws/credentials")
            self._git(repository, "commit", "--quiet", "-m", "fixture")

            with self.assertRaisesRegex(SnapshotRejected, "tracked sensitive path"):
                materialize(repository, root / "snapshot")

    def test_snapshot_rejects_a_sensitive_path_staged_for_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Snapshot Test")
            self._git(repository, "config", "user.email", "snapshot@example.invalid")
            (repository / ".aws").mkdir()
            (repository / ".aws" / "credentials").write_text("private\n", encoding="utf-8")
            self._git(repository, "add", ".aws/credentials")
            self._git(repository, "commit", "--quiet", "-m", "fixture")
            self._git(repository, "rm", "--quiet", ".aws/credentials")

            with self.assertRaisesRegex(SnapshotRejected, "tracked sensitive path"):
                materialize(repository, root / "snapshot")

    def test_snapshot_excludes_sensitive_paths_inside_permitted_submodules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "module"
            module.mkdir()
            self._git(module, "init", "--quiet")
            self._git(module, "config", "user.name", "Snapshot Test")
            self._git(module, "config", "user.email", "snapshot@example.invalid")
            (module / "public.txt").write_text("public\n", encoding="utf-8")
            self._git(module, "add", "public.txt")
            self._git(module, "commit", "--quiet", "-m", "fixture")
            (module / ".aws").mkdir()
            (module / ".aws" / "credentials").write_text("private\n", encoding="utf-8")

            repository = root / "repository"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Snapshot Test")
            self._git(repository, "config", "user.email", "snapshot@example.invalid")
            self._git(
                repository,
                "-c", "protocol.file.allow=always",
                "submodule", "add", "--quiet", str(module), "nmrpeak-upstream",
            )
            self._git(repository, "commit", "--quiet", "-am", "fixture")
            nested_sensitive = repository / "nmrpeak-upstream" / ".aws"
            nested_sensitive.mkdir()
            (nested_sensitive / "credentials").write_text("private\n", encoding="utf-8")

            snapshot = root / "snapshot"
            materialize(repository, snapshot)

            self.assertTrue((snapshot / "nmrpeak-upstream" / "public.txt").is_file())
            self.assertFalse((snapshot / "nmrpeak-upstream" / ".aws").exists())

    def test_snapshot_rejects_nested_submodules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            leaf = root / "leaf"
            leaf.mkdir()
            self._git(leaf, "init", "--quiet")
            self._git(leaf, "config", "user.name", "Snapshot Test")
            self._git(leaf, "config", "user.email", "snapshot@example.invalid")
            (leaf / "leaf.txt").write_text("leaf\n", encoding="utf-8")
            self._git(leaf, "add", "leaf.txt")
            self._git(leaf, "commit", "--quiet", "-m", "leaf")

            module = root / "module"
            module.mkdir()
            self._git(module, "init", "--quiet")
            self._git(module, "config", "user.name", "Snapshot Test")
            self._git(module, "config", "user.email", "snapshot@example.invalid")
            self._git(
                module, "-c", "protocol.file.allow=always",
                "submodule", "add", "--quiet", str(leaf), "nested",
            )
            self._git(module, "commit", "--quiet", "-am", "module")

            repository = root / "repository"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Snapshot Test")
            self._git(repository, "config", "user.email", "snapshot@example.invalid")
            self._git(
                repository, "-c", "protocol.file.allow=always",
                "submodule", "add", "--quiet", str(module), "nmrpeak-upstream",
            )
            self._git(repository, "commit", "--quiet", "-am", "root")

            with self.assertRaisesRegex(SnapshotRejected, "nested submodule path"):
                materialize(repository, root / "snapshot")

    def test_snapshot_rejects_an_internal_parent_symlink_into_sensitive_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Snapshot Test")
            self._git(repository, "config", "user.email", "snapshot@example.invalid")
            tracked_parent = repository / "models" / "a"
            tracked_parent.mkdir(parents=True)
            (tracked_parent / "fixture").write_text("public\n", encoding="utf-8")
            self._git(repository, "add", "models/a/fixture")
            self._git(repository, "commit", "--quiet", "-m", "fixture")
            shutil.rmtree(tracked_parent)
            sensitive = repository / ".aws"
            sensitive.mkdir()
            (sensitive / "fixture").write_text("private\n", encoding="utf-8")
            tracked_parent.symlink_to("../.aws")

            with self.assertRaisesRegex(SnapshotRejected, "symlinked parent"):
                materialize(repository, root / "snapshot")

    def test_worktree_snapshot_keeps_explicit_edits_and_excludes_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            worktree = root / "worktree"
            snapshot = root / "snapshot"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Snapshot Test")
            self._git(repository, "config", "user.email", "snapshot@example.invalid")
            (repository / ".gitignore").write_text("*.secret\n", encoding="utf-8")
            (repository / "tracked.txt").write_text("committed\n", encoding="utf-8")
            self._git(repository, "add", ".gitignore", "tracked.txt")
            self._git(repository, "commit", "--quiet", "-m", "fixture")
            self._git(repository, "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")
            (repository / "tracked.txt").write_text("new default branch head\n", encoding="utf-8")
            self._git(repository, "commit", "--quiet", "-am", "advance default branch")
            (worktree / "tracked.txt").write_text("edited\n", encoding="utf-8")
            (worktree / "visible.txt").write_text("explicit\n", encoding="utf-8")
            (worktree / "private.secret").write_text("excluded\n", encoding="utf-8")
            (worktree / ".aws").mkdir()
            (worktree / ".aws" / "credentials").write_text("also excluded\n", encoding="utf-8")

            digest = materialize(worktree, snapshot)

            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
            self.assertTrue((snapshot / ".git").is_dir())
            self.assertIn("tracked.txt", self._git(snapshot, "ls-files", "--cached").splitlines())
            self.assertEqual((snapshot / "tracked.txt").read_text(), "edited\n")
            self.assertEqual((snapshot / "visible.txt").read_text(), "explicit\n")
            self.assertFalse((snapshot / "private.secret").exists())
            self.assertFalse((snapshot / ".aws").exists())
            status = self._git(snapshot, "status", "--porcelain=v1", "--untracked-files=all")
            self.assertEqual(set(status.splitlines()), {" M tracked.txt", "?? visible.txt"})
            self.assertNotEqual(
                self._git(repository, "rev-parse", "--verify", "HEAD").strip(),
                self._git(worktree, "rev-parse", "--verify", "HEAD").strip(),
            )
            self.assertEqual(
                self._git(snapshot, "rev-parse", "--verify", "HEAD").strip(),
                self._git(worktree, "rev-parse", "--verify", "HEAD").strip(),
            )

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout
