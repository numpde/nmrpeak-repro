"""Exercise explicit and interactive acquisition-route selection."""

from pathlib import Path
import os
import pty
import select
import subprocess
import time
import unittest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/network-interface.sh"
COMMAND = f'source "{SCRIPT}"; select_network_interface; printf "selected=%s\\n" "$NETWORK_INTERFACE_SELECTED"'


class NetworkInterfaceTests(unittest.TestCase):
    def test_explicit_system_and_invalid_selections(self):
        for value, succeeds in (("system", True), ("", False), ("../bad", False)):
            with self.subTest(value=value):
                env = {**os.environ, "NMRPEAK_NETWORK_INTERFACE_INPUT_ORIGIN": "command line",
                       "NMRPEAK_NETWORK_INTERFACE_INPUT": value}
                result = subprocess.run(["bash", "-eu", "-c", COMMAND], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                if succeeds:
                    self.assertIn("selected=system", result.stdout)

    def test_headless_omission_and_ambient_selector_refuse(self):
        for origin in ("undefined", "environment"):
            with self.subTest(origin=origin):
                env = {**os.environ, "NMRPEAK_NETWORK_INTERFACE_INPUT_ORIGIN": origin,
                       "NMRPEAK_NETWORK_INTERFACE_INPUT": "system"}
                result = subprocess.run(["bash", "-eu", "-c", COMMAND], env=env,
                                        capture_output=True, text=True, start_new_session=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("selected=", result.stdout)

    def test_interactive_choice_has_no_default(self):
        pid, master = pty.fork()
        if pid == 0:
            env = {**os.environ, "NMRPEAK_NETWORK_INTERFACE_INPUT_ORIGIN": "undefined"}
            os.execvpe("bash", ["bash", "-eu", "-c", COMMAND], env)
        output = b""
        try:
            deadline = time.monotonic() + 10
            while b"Choice [" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 1)[0]:
                    output += os.read(master, 4096)
            self.assertIn(b"no default", output)
            os.write(master, b"1\n")
            while b"selected=system" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 1)[0]:
                    try:
                        output += os.read(master, 4096)
                    except OSError:
                        break
            self.assertIn(b"selected=system", output)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            pid = 0
        finally:
            os.close(master)
            if pid:
                os.kill(pid, 9)
                os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
