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
        interfaces = {path.name for path in Path("/sys/class/net").iterdir()}
        self.assertEqual(interfaces, {"lo"})
        root_mount = next(
            line for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
            if line.split()[4] == "/"
        )
        self.assertIn("ro", root_mount.split()[5].split(","))
        self.assertFalse(Path("/var/run/docker.sock").exists())

    def test_sensitive_checkout_paths_are_absent(self) -> None:
        for relative in (
            ".agents", ".aws", ".codex", "config/deployments", "secrets", "weights"
        ):
            self.assertFalse((Path("/workspace") / relative).exists(), relative)

    def test_sensitive_checkout_roots_are_not_tracked(self) -> None:
        tracked = subprocess.run(
            ("git", "-C", "/workspace", "ls-files", "-z"),
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode("utf-8").split("\0")
        sensitive = (".agents", ".aws", ".codex", "config/deployments", "secrets", "weights")
        exposed = sorted(
            path for path in tracked if path and any(
                path == root or path.startswith(root + "/") for root in sensitive
            )
        )
        self.assertEqual(exposed, [])
