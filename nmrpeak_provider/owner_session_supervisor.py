"""Fence one runner worker process to one provider-owned Unix socket session.

The supervisor owns listener, child, shutdown, and reap lifetimes. It observes
lifecycle signals but no protocol content; the child alone reads and writes
wire bytes, preventing supervision from becoming a second protocol owner.
"""

from __future__ import annotations

import fcntl
import logging
import math
import os
import select
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Sequence

from nmrpeak_provider.owner_session_endpoint import open_owner_session_directory


_SUPERVISOR_LOCK_NAME = ".owner-session.lock"
_OWNER_SESSION_LOST_STATUS = 72
_LOGGER = logging.getLogger("nmrpeak_runner.supervisor")
# Pure function tests import this module without adopting its process logging
# policy. The executable main configures the root sink explicitly below.
_LOGGER.addHandler(logging.NullHandler())
_LOG_FORMAT = "%(levelname)s %(name)s %(message)s"


class OwnerSessionLost(RuntimeError):
    """The owner disconnected after the supervisor reaped its worker."""

    def __init__(self, worker_returncode: int) -> None:
        super().__init__("owner session lost")
        self.worker_returncode = worker_returncode


class WorkerTerminationTimeout(RuntimeError):
    """The worker remained unreaped after the TERM and KILL budgets."""


class _SupervisorShutdown(RuntimeError):
    def __init__(self, signal_number: int) -> None:
        super().__init__(f"supervisor shutdown requested by signal {signal_number}")
        self.signal_number = signal_number


def supervise_owned_worker(
    *,
    owner_session_fd: int,
    worker_argv: Sequence[str],
    terminate_grace_seconds: float,
    kill_wait_seconds: float,
    shutdown_fd: int | None = None,
) -> int:
    """Run and reap one worker only while its owning stream remains connected.

    The epoll set joins owner closure, worker exit, and optional shutdown into
    one ordering point. Normal worker exit is reaped and returned. Owner loss
    and shutdown terminate and reap before raising distinct lifecycle signals;
    ``WorkerTerminationTimeout`` explicitly reports the exceptional path where
    reaping could not be proved.
    """
    argv = _validated_worker_argv(worker_argv)
    _require_timeout("terminate_grace_seconds", terminate_grace_seconds, allow_zero=True)
    _require_timeout("kill_wait_seconds", kill_wait_seconds, allow_zero=False)

    # Register before spawning so an invalid owner descriptor cannot leave a
    # worker behind. The watchdog observes only hangup/error bits; application
    # bytes remain exclusively owned by the worker on the shared descriptor.
    with select.epoll() as events:
        events.register(
            owner_session_fd,
            select.EPOLLRDHUP | select.EPOLLHUP | select.EPOLLERR,
        )
        if shutdown_fd is not None:
            events.register(shutdown_fd, select.EPOLLIN)
        worker: subprocess.Popen[bytes] | None = None
        pidfd = -1
        try:
            worker = subprocess.Popen(
                argv,
                close_fds=True,
                pass_fds=(owner_session_fd,),
            )
            _LOGGER.info("event=worker_started pid=%d", worker.pid)
            pidfd = os.pidfd_open(worker.pid)
            events.register(pidfd, select.EPOLLIN)
            observed = dict(events.poll())
            # One epoll batch has no event chronology. Process shutdown owns the
            # whole batch; otherwise a terminal worker result outranks owner loss.
            if shutdown_fd is not None and shutdown_fd in observed:
                signal_number = _read_shutdown_signal(shutdown_fd)
                _terminate_worker(
                    worker,
                    terminate_grace_seconds=terminate_grace_seconds,
                    kill_wait_seconds=kill_wait_seconds,
                )
                raise _SupervisorShutdown(signal_number)
            if pidfd in observed:
                returncode = worker.wait()
                if returncode == 0:
                    log = _LOGGER.info
                elif returncode == _OWNER_SESSION_LOST_STATUS:
                    log = _LOGGER.warning
                else:
                    log = _LOGGER.error
                log("event=worker_exited returncode=%d", returncode)
                return returncode
            if observed.get(owner_session_fd, 0):
                returncode = _terminate_worker(
                    worker,
                    terminate_grace_seconds=terminate_grace_seconds,
                    kill_wait_seconds=kill_wait_seconds,
                )
                raise OwnerSessionLost(returncode)
            raise RuntimeError("watchdog returned without an owner or worker event")
        except WorkerTerminationTimeout:
            # SIGKILL and the complete reap bound were already spent. A second
            # cleanup attempt would silently double the caller's hard limit.
            raise
        except BaseException:
            if worker is not None and worker.poll() is None:
                _terminate_worker(
                    worker,
                    terminate_grace_seconds=0,
                    kill_wait_seconds=kill_wait_seconds,
                )
            raise
        finally:
            if pidfd >= 0:
                os.close(pidfd)


def serve_one_owned_worker(
    *,
    socket_path: str,
    worker_argv: Sequence[str],
    terminate_grace_seconds: float,
    kill_wait_seconds: float,
    shutdown_fd: int | None = None,
) -> int:
    """Accept one private UDS owner and pass its stream to one serving worker.

    Runtime composition owns a stable name for the 0700 parent directory; this
    process deliberately trusts peers running under its own effective UID.
    """

    argv = _validated_worker_argv(worker_argv)
    _require_timeout("terminate_grace_seconds", terminate_grace_seconds, allow_zero=True)
    _require_timeout("kill_wait_seconds", kill_wait_seconds, allow_zero=False)
    parent_fd, socket_name, bind_path = _open_owner_session_bind_directory(
        socket_path
    )
    lock_fd = -1
    listener: socket.socket | None = None
    socket_identity: tuple[int, int] | None = None
    try:
        lock_fd = _acquire_supervisor_lock(parent_fd)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stale_status = os.stat(
                socket_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            stale_identity = (stale_status.st_dev, stale_status.st_ino)
            if (
                not stat.S_ISSOCK(stale_status.st_mode)
                or stale_status.st_uid != os.geteuid()
                or stat.S_IMODE(stale_status.st_mode) != 0o600
                or not _unlink_owned_socket(
                    parent_fd,
                    socket_name,
                    stale_identity,
                )
            ):
                raise FileExistsError(
                    f"refusing suspicious owner-session path: {socket_path}"
                )

        # AF_UNIX applies the process umask when creating its filesystem node.
        # Startup is single-threaded; restore the inherited mask immediately.
        previous_umask = os.umask(0o177)
        try:
            listener.bind(bind_path)
        finally:
            os.umask(previous_umask)

        socket_status = os.stat(
            socket_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        socket_identity = (socket_status.st_dev, socket_status.st_ino)
        if (
            not stat.S_ISSOCK(socket_status.st_mode)
            or socket_status.st_uid != os.geteuid()
            or stat.S_IMODE(socket_status.st_mode) != 0o600
        ):
            raise PermissionError("owner-session endpoint is not an owner-only socket")

        listener.listen(1)
        _LOGGER.info("event=endpoint_listening socket_path=%s", socket_path)
        if shutdown_fd is not None:
            with select.epoll() as events:
                events.register(listener.fileno(), select.EPOLLIN)
                events.register(shutdown_fd, select.EPOLLIN)
                observed = dict(events.poll())
            if shutdown_fd in observed:
                raise _SupervisorShutdown(_read_shutdown_signal(shutdown_fd))
            if listener.fileno() not in observed:
                raise RuntimeError("owner-session listener returned without an event")
        connection, _ = listener.accept()
        listener.close()
        if not _unlink_owned_socket(parent_fd, socket_name, socket_identity):
            connection.close()
            raise RuntimeError("owner-session endpoint changed before acquisition")
        _LOGGER.info("event=owner_attached")
        with connection:
            return supervise_owned_worker(
                owner_session_fd=connection.fileno(),
                worker_argv=(*argv, "--session-fd", str(connection.fileno())),
                terminate_grace_seconds=terminate_grace_seconds,
                kill_wait_seconds=kill_wait_seconds,
                shutdown_fd=shutdown_fd,
            )
    finally:
        if listener is not None:
            listener.close()
        if socket_identity is not None:
            _unlink_owned_socket(parent_fd, socket_name, socket_identity)
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(parent_fd)


def _acquire_supervisor_lock(parent_fd: int) -> int:
    """Fence stale-socket cleanup to one supervisor for this runtime directory."""

    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        lock_fd = os.open(
            _SUPERVISOR_LOCK_NAME,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
    except FileExistsError:
        lock_fd = os.open(
            _SUPERVISOR_LOCK_NAME,
            flags,
            dir_fd=parent_fd,
        )
    else:
        try:
            os.fchmod(lock_fd, 0o600)
        except BaseException:
            os.close(lock_fd)
            raise
    try:
        lock_status = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_status.st_mode)
            or lock_status.st_uid != os.geteuid()
            or stat.S_IMODE(lock_status.st_mode) != 0o600
        ):
            raise PermissionError(
                "owner-session supervisor lock is not an owner-only regular file"
            )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "another owner-session supervisor owns this runtime directory"
            ) from None
        return lock_fd
    except BaseException:
        os.close(lock_fd)
        raise


def _open_owner_session_bind_directory(
    socket_path: object,
) -> tuple[int, str, str]:
    parent_fd, socket_name = open_owner_session_directory(socket_path)
    try:
        if socket_name == _SUPERVISOR_LOCK_NAME:
            raise ValueError("owner-session socket path uses the reserved lock leaf")
        # The provider connects through socket_path, while bind uses the held
        # directory descriptor. Linux applies the same independent 108-byte
        # sockaddr_un field to both spellings.
        bind_path = f"/proc/self/fd/{parent_fd}/{socket_name}"
        if len(os.fsencode(bind_path)) > 107:
            raise ValueError(
                "owner-session socket leaf is too long for the held-directory bind path"
            )
        return parent_fd, socket_name, bind_path
    except BaseException:
        os.close(parent_fd)
        raise


def _unlink_owned_socket(
    parent_fd: int,
    socket_name: str,
    socket_identity: tuple[int, int],
) -> bool:
    try:
        status = os.stat(socket_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if (
        not stat.S_ISSOCK(status.st_mode)
        or (status.st_dev, status.st_ino) != socket_identity
    ):
        return False
    # The exact-mode directory excludes other OS principals. The identity check
    # keeps ordinary cleanup from removing a later path; same-UID processes are
    # deliberately inside this runtime's trust boundary.
    os.unlink(socket_name, dir_fd=parent_fd)
    return True


def _read_shutdown_signal(shutdown_fd: int) -> int:
    try:
        signal_bytes = os.read(shutdown_fd, 4096)
    except BlockingIOError:
        raise RuntimeError("shutdown event contained no signal") from None
    for signal_number in signal_bytes:
        if signal_number in (signal.SIGINT, signal.SIGTERM):
            return signal_number
    raise RuntimeError("shutdown event contained an unsupported signal")


def _terminate_worker(
    worker: subprocess.Popen[bytes],
    *,
    terminate_grace_seconds: float,
    kill_wait_seconds: float,
) -> int:
    if worker.poll() is not None:
        return worker.returncode
    worker.terminate()
    try:
        return worker.wait(timeout=terminate_grace_seconds)
    except subprocess.TimeoutExpired:
        worker.kill()
    try:
        return worker.wait(timeout=kill_wait_seconds)
    except subprocess.TimeoutExpired as error:
        raise WorkerTerminationTimeout(
            f"worker process {worker.pid} did not terminate"
        ) from error


def _require_timeout(name: str, value: float, *, allow_zero: bool) -> None:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")


def _validated_worker_argv(worker_argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(worker_argv, str):
        raise TypeError("worker_argv must be an argument sequence, not a string")
    argv = tuple(worker_argv)
    if not argv or not argv[0] or any(not isinstance(value, str) for value in argv):
        raise ValueError("worker_argv requires strings and a non-empty executable")
    return argv


def main(socket_path: str, argv: Sequence[str] | None = None) -> int:
    """Run the fence as the container's small pre-model process."""
    _configure_logging()
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 4 or arguments[2] != "--":
        raise SystemExit(
            "usage: owner_session_supervisor.py "
            "TERMINATE_GRACE_SECONDS KILL_WAIT_SECONDS -- WORKER [ARG ...]"
        )
    shutdown_read_fd, shutdown_write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    previous_wakeup_fd = signal.set_wakeup_fd(shutdown_write_fd)
    previous_handlers = {}
    try:
        previous_handlers = {
            signal_number: signal.signal(signal_number, _record_shutdown_signal)
            for signal_number in (signal.SIGINT, signal.SIGTERM)
        }
        return serve_one_owned_worker(
            socket_path=socket_path,
            worker_argv=arguments[3:],
            terminate_grace_seconds=float(arguments[0]),
            kill_wait_seconds=float(arguments[1]),
            shutdown_fd=shutdown_read_fd,
        )
    except OwnerSessionLost as error:
        _LOGGER.warning(
            "event=owner_session_lost worker_returncode=%d",
            error.worker_returncode,
        )
        return _OWNER_SESSION_LOST_STATUS
    except _SupervisorShutdown as shutdown:
        _LOGGER.info(
            "event=shutdown_requested signal=%d",
            shutdown.signal_number,
        )
        return 128 + shutdown.signal_number
    except WorkerTerminationTimeout as error:
        _LOGGER.error("event=worker_termination_timed_out detail=%r", str(error))
        return 1
    except Exception as error:
        _LOGGER.error(
            "event=supervisor_failed error_type=%s detail=%r",
            type(error).__name__,
            str(error),
        )
        return 1
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)
        signal.set_wakeup_fd(previous_wakeup_fd)
        os.close(shutdown_write_fd)
        os.close(shutdown_read_fd)


def _record_shutdown_signal(_signal_number: int, _frame: object) -> None:
    # signal.set_wakeup_fd writes the signal number into the supervisor pipe.
    # The handler must not perform lifecycle work asynchronously.
    pass


def _configure_logging() -> None:
    """Expose runner-owned INFO while leaving dependency verbosity at WARNING."""

    logging.basicConfig(level=logging.WARNING, format=_LOG_FORMAT, force=True)
    runner_logger = logging.getLogger("nmrpeak_runner")
    runner_logger.setLevel(logging.INFO)
