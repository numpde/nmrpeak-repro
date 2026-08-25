"""Prove the shared runner owner-session process lifecycle behavior."""

from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import runpy
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

import nmrpeak_provider.owner_session_supervisor as owner_session_supervisor


SUPERVISOR_PATH = Path(owner_session_supervisor.__file__).resolve()
OWNER_LOST_EXIT = 72
# The reviewed container is CPU-limited. This bounds test-harness observation;
# production termination limits remain the explicit arguments under test.
WAIT_SECONDS = 5


class OwnerSessionSupervisorTests(unittest.TestCase):
    def test_each_launcher_supplies_the_family_socket_identity(self) -> None:
        from nmrpeak_provider.runner_protocol import RUNNER_SOCKET_PATH

        for runner in ("nmrpeak_chf_v1", "nmrpeak_hf_v1"):
            with self.subTest(runner=runner):
                launcher = (
                    Path(__file__).resolve().parents[2]
                    / f"models/{runner}/runner/owner_session_supervisor.py"
                )
                with patch.object(
                    owner_session_supervisor,
                    "main",
                    return_value=73,
                ) as main, self.assertRaises(SystemExit) as raised:
                    runpy.run_path(str(launcher), run_name="__main__")

                self.assertEqual(raised.exception.code, 73)
                main.assert_called_once_with(RUNNER_SOCKET_PATH)

    def test_terminal_worker_exit_outranks_simultaneous_owner_loss(self) -> None:
        module = _load_supervisor()
        owner_fd = 10
        pidfd = 20
        worker = Mock(pid=123, wait=Mock(return_value=7), poll=Mock(return_value=7))
        events = MagicMock()
        events.__enter__.return_value = events
        events.poll.return_value = [
            (owner_fd, select.EPOLLHUP),
            (pidfd, select.EPOLLIN),
        ]

        with patch.object(module.select, "epoll", return_value=events), patch.object(
            module.subprocess, "Popen", return_value=worker
        ), patch.object(module.os, "pidfd_open", return_value=pidfd), patch.object(
            module.os, "close"
        ) as close, patch.object(module, "_terminate_worker") as terminate:
            result = module.supervise_owned_worker(
                owner_session_fd=owner_fd,
                worker_argv=("worker",),
                terminate_grace_seconds=1,
                kill_wait_seconds=1,
            )

        self.assertEqual(result, 7)
        worker.wait.assert_called_once_with()
        terminate.assert_not_called()
        close.assert_called_once_with(pidfd)

    def test_process_shutdown_outranks_other_events_in_the_same_batch(self) -> None:
        module = _load_supervisor()
        owner_fd = 10
        shutdown_fd = 11
        pidfd = 20
        worker = Mock(pid=123, wait=Mock(), poll=Mock(return_value=7))
        events = MagicMock()
        events.__enter__.return_value = events
        events.poll.return_value = [
            (owner_fd, select.EPOLLHUP),
            (pidfd, select.EPOLLIN),
            (shutdown_fd, select.EPOLLIN),
        ]

        with patch.object(module.select, "epoll", return_value=events), patch.object(
            module.subprocess, "Popen", return_value=worker
        ), patch.object(module.os, "pidfd_open", return_value=pidfd), patch.object(
            module.os, "close"
        ) as close, patch.object(
            module, "_read_shutdown_signal", return_value=signal.SIGTERM
        ), patch.object(
            module, "_terminate_worker", return_value=7
        ) as terminate, self.assertRaises(module._SupervisorShutdown) as raised:
            module.supervise_owned_worker(
                owner_session_fd=owner_fd,
                worker_argv=("worker",),
                terminate_grace_seconds=1,
                kill_wait_seconds=1,
                shutdown_fd=shutdown_fd,
            )

        self.assertEqual(raised.exception.signal_number, signal.SIGTERM)
        terminate.assert_called_once_with(
            worker,
            terminate_grace_seconds=1,
            kill_wait_seconds=1,
        )
        worker.wait.assert_not_called()
        close.assert_called_once_with(pidfd)

    def test_preserves_normal_worker_exit_status(self) -> None:
        owner, worker = socket.socketpair()
        with owner, worker:
            supervisor = _start_supervisor(
                worker,
                [sys.executable, __file__, "worker-exit", "7"],
            )
            try:
                worker.close()
                self.assertEqual(supervisor.wait(timeout=WAIT_SECONDS), 7)
            finally:
                owner.close()
                _stop_process(supervisor)

    def test_private_endpoint_rejects_an_unsafe_parent_directory(self) -> None:
        module = _load_supervisor()
        worker_argv = [sys.executable, __file__, "worker-exit", "0"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "owner.sock"
            os.chmod(root, 0o755)

            with self.assertRaises(PermissionError):
                module.serve_one_owned_worker(
                    socket_path=str(socket_path),
                    worker_argv=worker_argv,
                    terminate_grace_seconds=0.05,
                    kill_wait_seconds=1,
                )

    def test_private_endpoint_preserves_an_existing_regular_file(self) -> None:
        module = _load_supervisor()
        worker_argv = [sys.executable, __file__, "worker-exit", "0"]
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "owner.sock"
            socket_path.write_text("keep", encoding="ascii")

            with self.assertRaises(FileExistsError):
                module.serve_one_owned_worker(
                    socket_path=str(socket_path),
                    worker_argv=worker_argv,
                    terminate_grace_seconds=0.05,
                    kill_wait_seconds=1,
                )
            self.assertEqual(socket_path.read_text(encoding="ascii"), "keep")

    def test_private_endpoint_rejects_an_unsafe_supervisor_lock(self) -> None:
        module = _load_supervisor()
        worker_argv = [sys.executable, __file__, "worker-exit", "0"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "owner.sock"
            lock_path = root / ".owner-session.lock"
            lock_path.touch()
            lock_path.chmod(0o640)

            with self.assertRaises(PermissionError):
                module.serve_one_owned_worker(
                    socket_path=str(socket_path),
                    worker_argv=worker_argv,
                    terminate_grace_seconds=0.05,
                    kill_wait_seconds=1,
                )
            self.assertFalse(socket_path.exists())

    def test_private_endpoint_preserves_a_suspicious_socket(self) -> None:
        module = _load_supervisor()
        worker_argv = [sys.executable, __file__, "worker-exit", "0"]
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "owner.sock"
            suspicious_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            suspicious_socket.bind(str(socket_path))
            os.chmod(socket_path, 0o660)
            try:
                with self.assertRaises(FileExistsError):
                    module.serve_one_owned_worker(
                        socket_path=str(socket_path),
                        worker_argv=worker_argv,
                        terminate_grace_seconds=0.05,
                        kill_wait_seconds=1,
                    )
                self.assertTrue(stat.S_ISSOCK(socket_path.lstat().st_mode))
            finally:
                suspicious_socket.close()
                socket_path.unlink()

    def test_private_endpoint_rejects_the_reserved_lock_name(self) -> None:
        module = _load_supervisor()
        worker_argv = [sys.executable, __file__, "worker-exit", "0"]
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / ".owner-session.lock"

            with self.assertRaisesRegex(ValueError, "reserved lock leaf"):
                module.serve_one_owned_worker(
                    socket_path=str(lock_path),
                    worker_argv=worker_argv,
                    terminate_grace_seconds=0.05,
                    kill_wait_seconds=1,
                )

    def test_private_endpoint_rejects_symlink_directory_traversal(self) -> None:
        module = _load_supervisor()
        worker_argv = [sys.executable, __file__, "worker-exit", "0"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o700)
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaises(ValueError):
                module.serve_one_owned_worker(
                    socket_path=str(link / "owner.sock"),
                    worker_argv=worker_argv,
                    terminate_grace_seconds=0.05,
                    kill_wait_seconds=1,
                )

    def test_private_endpoint_rejects_a_relative_path(self) -> None:
        module = _load_supervisor()
        with self.assertRaises(ValueError):
            module.serve_one_owned_worker(
                socket_path="relative.sock",
                worker_argv=[sys.executable, __file__, "worker-exit", "0"],
                terminate_grace_seconds=0.05,
                kill_wait_seconds=1,
            )

    def test_private_endpoint_rejects_a_leaf_too_long_for_held_bind(self) -> None:
        module = _load_supervisor()
        short_root = Path("/tmp") / f"u{os.getpid():x}"
        short_root.mkdir(mode=0o700)
        try:
            maximum_leaf = "x" * (
                107 - len(os.fsencode(str(short_root))) - 1
            )
            with self.assertRaisesRegex(ValueError, "held-directory bind path"):
                module.serve_one_owned_worker(
                    socket_path=str(short_root / maximum_leaf),
                    worker_argv=[sys.executable, __file__, "worker-exit", "0"],
                    terminate_grace_seconds=0.05,
                    kill_wait_seconds=1,
                )
        finally:
            short_root.rmdir()

    def test_recovers_an_admissible_stale_socket_under_its_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "owner.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            stale.close()

            supervisor = _start_private_supervisor(
                socket_path,
                [sys.executable, __file__, "worker-exit", "0"],
            )
            owner: socket.socket | None = None
            try:
                _wait_for_private_endpoint(
                    supervisor,
                    socket_path,
                    deadline=time.monotonic() + WAIT_SECONDS,
                )
                owner = _connect_private_endpoint(supervisor, socket_path)
                self.assertEqual(supervisor.wait(timeout=WAIT_SECONDS), 0)
                lock_path = Path(directory) / ".owner-session.lock"
                lock_status = lock_path.lstat()
                self.assertTrue(stat.S_ISREG(lock_status.st_mode))
                self.assertEqual(stat.S_IMODE(lock_status.st_mode), 0o600)
            finally:
                if owner is not None:
                    owner.close()
                _stop_process(supervisor)

    def test_competing_supervisor_cannot_replace_the_live_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "owner.sock"
            first = _start_private_supervisor(
                socket_path,
                [sys.executable, __file__, "worker-exit", "0"],
            )
            second: subprocess.Popen[str] | None = None
            owner: socket.socket | None = None
            try:
                _wait_for_private_endpoint(
                    first,
                    socket_path,
                    deadline=time.monotonic() + WAIT_SECONDS,
                )
                live_identity = (
                    socket_path.lstat().st_dev,
                    socket_path.lstat().st_ino,
                )
                second = _start_private_supervisor(
                    socket_path,
                    [sys.executable, __file__, "worker-exit", "0"],
                )
                self.assertNotEqual(second.wait(timeout=WAIT_SECONDS), 0)
                self.assertEqual(
                    (socket_path.lstat().st_dev, socket_path.lstat().st_ino),
                    live_identity,
                )
                owner = _connect_private_endpoint(first, socket_path)
                self.assertEqual(first.wait(timeout=WAIT_SECONDS), 0)
            finally:
                if owner is not None:
                    owner.close()
                if second is not None:
                    _stop_process(second)
                _stop_process(first)

    def test_private_owner_loss_reaps_worker_and_preserves_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "owner.sock"
            supervisor = _start_private_supervisor(
                socket_path,
                [sys.executable, __file__, "worker-block"],
                stdout=subprocess.PIPE,
            )
            owner: socket.socket | None = None
            worker_pidfd = -1
            try:
                owner = _connect_private_endpoint(supervisor, socket_path)
                assert supervisor.stdout is not None
                readable, _, _ = select.select(
                    [supervisor.stdout], [], [], WAIT_SECONDS
                )
                self.assertTrue(readable, "blocked worker did not report readiness")
                worker_pidfd = os.pidfd_open(int(supervisor.stdout.readline()))
                self.assertFalse(socket_path.exists())
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as second:
                    with self.assertRaises(FileNotFoundError):
                        second.connect(str(socket_path))

                socket_path.write_text("replacement", encoding="ascii")
                owner.close()
                self.assertEqual(
                    supervisor.wait(timeout=WAIT_SECONDS), OWNER_LOST_EXIT
                )
                readable, _, _ = select.select([worker_pidfd], [], [], 1)
                self.assertTrue(readable, "owner loss did not reap the worker")
                self.assertEqual(
                    socket_path.read_text(encoding="ascii"),
                    "replacement",
                )
            finally:
                if owner is not None:
                    owner.close()
                if worker_pidfd >= 0:
                    _stop_pidfd(worker_pidfd)
                    os.close(worker_pidfd)
                _stop_process(supervisor)

    def test_private_endpoint_shutdown_before_owner_cleans_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "owner.sock"
            supervisor = _start_private_supervisor(
                socket_path,
                [sys.executable, __file__, "worker-block"],
            )
            try:
                _wait_for_private_endpoint(
                    supervisor,
                    socket_path,
                    deadline=time.monotonic() + WAIT_SECONDS,
                )
                supervisor.send_signal(signal.SIGTERM)
                self.assertEqual(supervisor.wait(timeout=WAIT_SECONDS), 143)
                self.assertFalse(socket_path.exists())
            finally:
                _stop_process(supervisor)

    def test_private_endpoint_signals_reap_active_worker(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal_number=signal_number):
                with tempfile.TemporaryDirectory() as directory:
                    socket_path = Path(directory) / "owner.sock"
                    supervisor = _start_private_supervisor(
                        socket_path,
                        [sys.executable, __file__, "worker-block"],
                        stdout=subprocess.PIPE,
                    )
                    owner: socket.socket | None = None
                    worker_pidfd = -1
                    try:
                        owner = _connect_private_endpoint(supervisor, socket_path)
                        assert supervisor.stdout is not None
                        readable, _, _ = select.select(
                            [supervisor.stdout], [], [], WAIT_SECONDS
                        )
                        self.assertTrue(
                            readable,
                            "blocked worker did not report readiness",
                        )
                        worker_pidfd = os.pidfd_open(
                            int(supervisor.stdout.readline())
                        )

                        supervisor.send_signal(signal_number)
                        self.assertEqual(
                            supervisor.wait(timeout=WAIT_SECONDS),
                            128 + signal_number,
                        )
                        readable, _, _ = select.select([worker_pidfd], [], [], 1)
                        self.assertTrue(readable, "shutdown did not reap the worker")
                        self.assertFalse(socket_path.exists())
                    finally:
                        if owner is not None:
                            owner.close()
                        if worker_pidfd >= 0:
                            _stop_pidfd(worker_pidfd)
                            os.close(worker_pidfd)
                        _stop_process(supervisor)

    def test_owner_loss_kills_and_reaps_blocked_worker_without_reading(self) -> None:
        owner, worker = socket.socketpair()
        observer = worker.dup()
        observer.settimeout(1)
        with owner, worker, observer:
            supervisor = _start_supervisor(
                worker,
                [sys.executable, __file__, "worker-block"],
                stdout=subprocess.PIPE,
            )
            worker_pidfd = -1
            try:
                worker.close()
                assert supervisor.stdout is not None
                readable, _, _ = select.select(
                    [supervisor.stdout], [], [], WAIT_SECONDS
                )
                self.assertTrue(readable, "blocked worker did not report readiness")
                worker_pid = int(supervisor.stdout.readline())
                # Failure cleanup must remain bound to this process rather than
                # signaling a numeric PID that the kernel could later reuse.
                worker_pidfd = os.pidfd_open(worker_pid)

                owner.sendall(b"application-byte")
                owner.close()

                self.assertEqual(
                    supervisor.wait(timeout=WAIT_SECONDS), OWNER_LOST_EXIT
                )
                self.assertEqual(
                    observer.recv(16, socket.MSG_PEEK),
                    b"application-byte",
                )
            finally:
                owner.close()
                if worker_pidfd >= 0:
                    try:
                        _stop_pidfd(worker_pidfd)
                    finally:
                        os.close(worker_pidfd)
                _stop_process(supervisor)


def _start_supervisor(
    owner_session: socket.socket,
    worker_argv: list[str],
    *,
    stdout: int | None = None,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            __file__,
            "supervise",
            str(owner_session.fileno()),
            *worker_argv,
        ],
        pass_fds=(owner_session.fileno(),),
        stdout=stdout,
        text=True,
    )


def _start_private_supervisor(
    socket_path: Path,
    worker_argv: list[str],
    *,
    stdout: int | None = None,
    stderr: int | None = subprocess.DEVNULL,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            __file__,
            "private-supervisor",
            str(socket_path),
            "0.05",
            "1",
            "--",
            *worker_argv,
        ],
        stdout=stdout,
        stderr=stderr,
        text=True,
    )


def _connect_private_endpoint(
    supervisor: subprocess.Popen[str],
    socket_path: Path,
) -> socket.socket:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        _wait_for_private_endpoint(supervisor, socket_path, deadline=deadline)
        owner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            owner.connect(str(socket_path))
            return owner
        except (FileNotFoundError, ConnectionRefusedError):
            owner.close()
        except BaseException:
            owner.close()
            raise
        if time.monotonic() >= deadline:
            raise AssertionError("private endpoint did not accept its owner")
        time.sleep(0.01)


def _wait_for_private_endpoint(
    supervisor: subprocess.Popen[str],
    socket_path: Path,
    *,
    deadline: float,
) -> None:
    while True:
        if supervisor.poll() is not None:
            raise AssertionError("private endpoint supervisor exited before bind")
        try:
            status = socket_path.lstat()
        except FileNotFoundError:
            status = None
        if status is not None:
            if not stat.S_ISSOCK(status.st_mode):
                raise AssertionError("private endpoint is not a socket")
            if stat.S_IMODE(status.st_mode) != 0o600:
                raise AssertionError("private endpoint is not mode 0600")
            return
        if time.monotonic() >= deadline:
            raise AssertionError("private endpoint did not appear")
        time.sleep(0.01)


def _load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "owner_session_supervisor",
        SUPERVISOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SUPERVISOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_helper(argv: list[str]) -> int:
    mode, *arguments = argv
    if mode == "worker-exit":
        return int(arguments[0])
    if mode == "worker-block":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print(os.getpid(), flush=True)
        while True:
            signal.pause()
    if mode == "supervise":
        module = _load_supervisor()
        try:
            return module.supervise_owned_worker(
                owner_session_fd=int(arguments[0]),
                worker_argv=arguments[1:],
                terminate_grace_seconds=0.05,
                kill_wait_seconds=1,
            )
        except module.OwnerSessionLost as error:
            if error.worker_returncode != -signal.SIGKILL:
                return 73
            return OWNER_LOST_EXIT
    if mode == "private-supervisor":
        module = _load_supervisor()
        return module.main(arguments[0], arguments[1:])
    raise ValueError(f"unknown helper mode: {mode}")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _stop_pidfd(pidfd: int) -> None:
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    except ProcessLookupError:
        pass


if __name__ == "__main__" and len(sys.argv) > 1:
    raise SystemExit(_run_helper(sys.argv[1:]))
