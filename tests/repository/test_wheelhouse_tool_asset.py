"""Exercise shared tool acquisition without network access."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from repository_checks.wheelhouse_tool_asset import TransientToolDownload, _download, ensure_tool


class WheelhouseToolAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.pool = Path(temporary.name)
        self.name = "tool.pyz"
        self.content = b"pinned tool"
        self.digest = sha256(self.content).hexdigest()

    def ensure(self, interface, download):
        return ensure_tool(self.pool, interface, name=self.name, expected=self.digest, download=download)

    def test_cache_hit_needs_neither_route_nor_download(self) -> None:
        path = self.pool / self.name
        path.write_bytes(self.content)
        inode = path.stat().st_ino
        self.assertTrue(self.ensure(None, lambda *_: self.fail("unexpected download")))
        self.assertEqual(path.stat().st_ino, inode)

    def test_absent_tool_does_not_download_without_a_route(self) -> None:
        self.assertFalse(self.ensure(None, lambda *_: self.fail("unexpected download")))
        self.assertEqual(list(self.pool.iterdir()), [])

    def test_cached_tool_is_readable_while_another_reader_holds_the_pool(self) -> None:
        (self.pool / self.name).write_bytes(self.content)
        directory = os.open(self.pool, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory, fcntl.LOCK_SH)
            code = ("from pathlib import Path\n"
                    "from repository_checks.wheelhouse_tool_asset import ensure_tool\n"
                    f"assert ensure_tool(Path({str(self.pool)!r}), None, name={self.name!r}, expected={self.digest!r})\n")
            subprocess.run([sys.executable, "-c", code], check=True, timeout=5)
        finally:
            os.close(directory)

    def test_conflict_is_preserved_without_download(self) -> None:
        path = self.pool / self.name
        path.write_bytes(b"other")
        inode = path.stat().st_ino
        with self.assertRaisesRegex(ValueError, "preserved"):
            self.ensure("system", lambda *_: self.fail("unexpected download"))
        self.assertEqual((path.read_bytes(), path.stat().st_ino), (b"other", inode))

    def test_download_verifies_before_publication_and_cleans_failures(self) -> None:
        def fetch(output, _directory, interface):
            self.assertEqual(interface, "system")
            os.write(output, self.content)

        self.assertTrue(self.ensure("system", fetch))
        self.assertEqual((self.pool / self.name).read_bytes(), self.content)
        self.assertEqual([path.name for path in self.pool.iterdir()], [self.name])
        (self.pool / self.name).unlink()
        with self.assertRaisesRegex(ValueError, "differs"):
            self.ensure("system", lambda output, *_: os.write(output, b"wrong"))
        self.assertEqual(list(self.pool.iterdir()), [])

    def test_competing_callers_fetch_once(self) -> None:
        requests = []

        def fetch(output, _directory, _interface):
            requests.append(1)
            time.sleep(0.1)
            os.write(output, self.content)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.ensure("system", fetch), range(2)))
        self.assertEqual(results, [True, True])
        self.assertEqual(requests, [1])
        self.assertEqual((self.pool / self.name).read_bytes(), self.content)

    def test_curl_receives_only_the_selected_route(self) -> None:
        with patch("repository_checks.wheelhouse_tool_asset.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stderr=b"200|000|")
            _download(3, 4, "wlan0")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--interface") + 1], "wlan0")
        self.assertIn("--proto-redir", command)
        self.assertEqual(run.call_args.kwargs["pass_fds"], (4,))

    def test_curl_classifies_retryable_and_terminal_failures_without_raw_text(self) -> None:
        cases = (
            (22, b"429|000|5", TransientToolDownload, "HTTP 429", 5),
            (22, b"503|000|", TransientToolDownload, "HTTP 503", 0),
            (56, b"000|502|", TransientToolDownload, "HTTP 502", 0),
            (56, b"000|503|", ValueError, "Retry-After is unavailable", None),
            (56, b"000|403|", ValueError, "proxy tunnel HTTP 403", None),
            (56, b"000|407|", ValueError, "proxy tunnel HTTP 407", None),
            (28, b"000|000|", TransientToolDownload, "transfer failed", 0),
            (22, b"404|000|", ValueError, "HTTP 404", None),
            (6, b"000|000|", ValueError, "name resolution failed", None),
            (60, b"000|000|", ValueError, "TLS verification failed", None),
            (23, b"200|000|", ValueError, "status 23", None),
            (22, b"429|000|31", ValueError, "exceeds 30 seconds", None),
            (22, b"429|000|private-marker", ValueError, "invalid Retry-After", None),
        )
        for code, metadata, error_type, message, delay in cases:
            with self.subTest(code=code, metadata=metadata), \
                 patch("repository_checks.wheelhouse_tool_asset.subprocess.run",
                       return_value=subprocess.CompletedProcess([], code, stderr=metadata)):
                with self.assertRaisesRegex(error_type, message) as captured:
                    _download(3, 4, "system")
                self.assertNotIn('private-marker', str(captured.exception))
                if delay is not None:
                    self.assertEqual(captured.exception.retry_after, delay)

    def test_transient_attempts_use_fresh_partials_and_bounded_backoff(self) -> None:
        partials = []
        def fetch(output, _directory, _interface):
            current, = self.pool.glob('*.part')
            self.assertEqual(os.fstat(output).st_size, 0)
            partials.append(current.name)
            if len(partials) < 3:
                os.write(output, b'incomplete')
                raise TransientToolDownload('HTTP 503', 5 if len(partials) == 1 else 0)
            os.write(output, self.content)
        with patch('repository_checks.wheelhouse_tool_asset.time.sleep') as sleep:
            self.assertTrue(self.ensure('system', fetch))
        self.assertEqual(len(set(partials)), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 2])
        self.assertEqual((self.pool / self.name).read_bytes(), self.content)
        self.assertEqual(list(self.pool.glob('*.part')), [])

    def test_exhaustion_and_failed_cleanup_never_start_an_extra_attempt(self) -> None:
        attempts = []
        def fetch(output, _directory, _interface):
            attempts.append(1)
            os.write(output, b'incomplete')
            raise TransientToolDownload('transfer failed')
        with patch('repository_checks.wheelhouse_tool_asset.time.sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'after 3 attempts'):
                self.ensure('system', fetch)
        self.assertEqual(len(attempts), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertEqual(list(self.pool.glob('*.part')), [])
        attempts.clear()
        with patch('repository_checks.wheelhouse_tool_asset.os.unlink', side_effect=PermissionError):
            with self.assertRaisesRegex(OSError, 'incomplete wheelhouse tool file'):
                self.ensure('system', fetch)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(list(self.pool.glob('*.part'))), 1)

    def test_termination_reaps_downloader_and_removes_its_partial(self) -> None:
        helper = self.pool / "bin"
        helper.mkdir()
        pidfile = self.pool / "child.pid"
        curl = helper / "curl"
        curl.write_text(f"#!{sys.executable}\nimport os,time\n"
                        "open(os.environ['TEST_CURL_PID'], 'w').write(str(os.getpid()))\n"
                        "time.sleep(60)\n")
        curl.chmod(0o755)
        environment = os.environ | {"PATH": str(helper) + os.pathsep + os.environ["PATH"],
                                    "TEST_CURL_PID": str(pidfile)}
        process = subprocess.Popen(
            (sys.executable, "-m", "repository_checks.wheelhouse_tool_asset", "acquire",
             str(self.pool), "--interface", "system"),
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pidfile.exists(), "fake downloader did not start")
            child = int(pidfile.read_text())
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            self.assertEqual(list(self.pool.glob("*.part")), [])
            self.assertFalse(self.ensure(None, lambda *_: self.fail("unexpected download")))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_termination_during_verification_removes_its_partial(self) -> None:
        helper = self.pool / "bin"
        helper.mkdir()
        marker = self.pool / "verifying"
        curl = helper / "curl"
        curl.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'partial')\n")
        curl.chmod(0o755)
        environment = os.environ | {"PATH": str(helper) + os.pathsep + os.environ["PATH"],
                                    "TEST_VERIFY_MARKER": str(marker)}
        code = (
            "import os,time\nfrom pathlib import Path\n"
            "from repository_checks import wheelhouse_tool_asset as tool\n"
            "def wait(*args, **kwargs):\n"
            " Path(os.environ['TEST_VERIFY_MARKER']).touch()\n time.sleep(60)\n"
            "tool.hashlib.file_digest = wait\nraise SystemExit(tool.main())\n"
        )
        process = subprocess.Popen(
            (sys.executable, "-c", code, "acquire", str(self.pool), "--interface", "system"),
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "download did not reach verification")
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(list(self.pool.glob("*.part")), [])
            self.assertFalse(self.ensure(None, lambda *_: self.fail("unexpected download")))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
