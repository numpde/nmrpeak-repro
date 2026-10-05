from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.environ.get("NMRPEAK_TEST_CONTAINER") == "1", "container-only posture proof")
class ContainerPostureTests(unittest.TestCase):
    def test_runtime_is_nonroot_unprivileged_offline_and_read_only(self) -> None:
        self.assertNotIn(os.getuid(), (0, 65532))
        self.assertNotEqual(os.getgid(), 0)
        status = Path("/proc/self/status").read_text(encoding="ascii")
        self.assertIn("CapEff:\t0000000000000000", status)
        self.assertIn("NoNewPrivs:\t1", status)
        interfaces = {path.name for path in Path("/sys/class/net").iterdir()}
        self.assertEqual(interfaces, {"lo"})
        root_mount = next(
            line for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
            if line.split()[4] == "/"
        )
        self.assertIn("ro", root_mount.split()[5].split(","))
        self.assertFalse(Path("/var/run/docker.sock").exists())

    def test_tmpfs_and_cgroup_limits_are_kernel_enforced(self) -> None:
        tmp_mount = next(
            line for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
            if line.split()[4] == "/tmp"
        )
        fields, filesystem = tmp_mount.split(" - ", 1)
        mount_options = set(fields.split()[5].split(","))
        filesystem_fields = filesystem.split()
        super_options = set(filesystem_fields[2].split(","))
        self.assertEqual(filesystem_fields[0], "tmpfs")
        self.assertTrue({"rw", "nosuid", "nodev"} <= mount_options | super_options)
        self.assertNotIn("noexec", mount_options | super_options)

        cgroup = Path("/sys/fs/cgroup")
        self.assertEqual((cgroup / "pids.max").read_text().strip(), "256")
        self.assertEqual((cgroup / "memory.max").read_text().strip(), "1073741824")
        self.assertEqual((cgroup / "memory.swap.max").read_text().strip(), "0")
        self.assertEqual((cgroup / "cpu.max").read_text().strip(), "200000 100000")

    def test_sensitive_checkout_paths_are_absent(self) -> None:
        for relative in (
            ".agents", ".aws", ".codex", "config/deployments", "secrets", "weights"
        ):
            self.assertFalse((Path("/workspace") / relative).exists(), relative)

    def test_checkout_git_stores_contain_only_the_snapshot_commit(self) -> None:
        for repository in (Path("/workspace"), Path("/workspace/nmrpeak-upstream"), Path("/workspace/unicore-upstream")):
            if not repository.exists():
                continue
            shallow = subprocess.run(
                ("git", "-C", str(repository), "rev-parse", "--is-shallow-repository"),
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(shallow, "true", repository)
            parent = subprocess.run(
                ("git", "-C", str(repository), "rev-parse", "--verify", "HEAD^"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.assertNotEqual(parent.returncode, 0, repository)
            refs = subprocess.run(
                ("git", "-C", str(repository), "for-each-ref", "--format=%(refname)"),
                check=True, capture_output=True, text=True,
            ).stdout
            self.assertEqual(refs, "", repository)
            unreachable = subprocess.run(
                ("git", "-C", str(repository), "fsck", "--unreachable", "--no-reflogs", "--no-progress"),
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(unreachable.stdout, "", repository)
