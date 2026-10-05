from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from repository_checks.test_snapshot import SnapshotRejected, materialize


class TestSnapshotTests(unittest.TestCase):
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
