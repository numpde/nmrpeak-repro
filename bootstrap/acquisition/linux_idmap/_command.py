"""Supervise one Docker CLI process and its private process group."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from typing import Mapping

from ._probe_model import Cancellation

# Combined stdout/stderr limit for each client command.
# repo-check: allow-module-constant
OUTPUT_LIMIT = 65536


class DockerError(Exception):
    """A stable, redacted failure; uncertain mutation must be reconciled."""

    # silent-defaults: allow-contract -- Dispatch marks uncertain mutations; failures before dispatch do not imply creation.
    def __init__(self, category: str, message: str, *, mutation_uncertain: bool = False):
        super().__init__(message)
        self.category = category
        self.mutation_uncertain = mutation_uncertain


# silent-defaults: allow-contract -- Read-only commands are the transport baseline; mutation callers explicitly opt into uncertain-dispatch reporting; tests/test_docker.py::CommandTests.
def _run_command(argv: list[str], *, env: Mapping[str, str], timeout: float,
                 cancel: Cancellation | None = None, mutation: bool = False) -> str:
    """Bound both output pipes; signal the private group and reap its leader.

    This operation exclusively owns waiting for its child: no SIGCHLD auto-reap
    or competing waiter. It confirms only the direct child's exit, not every
    descendant's exit. An interrupted spawn may return no child handle; its
    mutation remains uncertain. Client exit never establishes engine-side outcome.
    """
    deadline = _deadline(timeout)
    if cancel is not None and cancel.is_set():
        raise DockerError("cancelled", "Docker operation was cancelled before dispatch.")
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        raise DockerError("invalid_policy", "Docker client supervision requires waitable children.")
    # Allocate the polling resource before spawn: a selector allocation failure
    # must not strand a client that may already have submitted a mutation.
    try:
        selector = selectors.DefaultSelector()
    except OSError as exc:
        raise DockerError("client_unavailable", "Cannot prepare bounded Docker client supervision.") from exc
    output = bytearray()
    size = 0
    spawn_requested = False
    try:
        # Preparation may consume the budget or receive cancellation.
        if cancel is not None and cancel.is_set():
            raise DockerError("cancelled", "Docker operation was cancelled before dispatch.")
        _remaining(deadline)
        spawn_requested = True
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=dict(env), start_new_session=True)
    except BaseException as error:
        # Even OSError can come from a signal handler after Popen created a child.
        uncertain = mutation and spawn_requested
        operation = error
        if spawn_requested and isinstance(error, OSError):
            operation = DockerError("docker_unavailable", "Docker client startup did not complete.",
                                    mutation_uncertain=uncertain)
            operation.__cause__ = error
        cleanup = _close_error(selector)
        if cleanup is not None:
            raise _command_failure(operation, cleanup, mutation_uncertain=uncertain) from operation
        if spawn_requested and operation is error and isinstance(error, (DockerError, BaseExceptionGroup)):
            raise _command_failure(error, mutation_uncertain=uncertain) from error
        raise operation
    operation_error = retirement_error = None
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        events = ()
        while True:
            if cancel is not None and cancel.is_set():
                raise DockerError("cancelled", "Docker operation was cancelled.")
            for key, _ in events:
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                size += len(chunk)
                if size > OUTPUT_LIMIT:
                    raise DockerError("output_limit", "Docker command exceeded its output bound.")
                if key.fileobj is process.stdout:
                    output.extend(chunk)
            exited = _peek_exit(process)
            if exited is not None and not selector.get_map():
                break
            events = selector.select(min(0.05, _remaining(deadline)))
        if exited.si_code != os.CLD_EXITED or exited.si_status != 0:
            raise DockerError("docker_command", "Docker command did not report success.")
        try:
            decoded = output.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise DockerError("invalid_response", "Docker command returned non-UTF-8 output.") from exc
    except BaseException as error:
        operation_error = error
    finally:
        try:
            _terminate(process)
        except BaseException as error:
            retirement_error = error
        finally:
            # Evaluate every close eagerly, even when an earlier one fails.
            close_errors = [_close_error(selector), _close_error(process.stdout), _close_error(process.stderr)]
    errors = [error for error in (operation_error, retirement_error, *close_errors) if error is not None]
    if errors:
        first = errors[0]
        if len(errors) > 1 or isinstance(first, BaseExceptionGroup):
            raise _command_failure(*errors, mutation_uncertain=mutation) from first
        if isinstance(first, DockerError):
            first.mutation_uncertain = mutation
        raise first
    return decoded


def _command_failure(*errors: BaseException, mutation_uncertain: bool) -> BaseExceptionGroup:
    """Preserve ordered failures and dispatch uncertainty in an exception group."""
    failure = BaseExceptionGroup("Docker client failures.", errors)
    failure.mutation_uncertain = mutation_uncertain
    return failure


def _close_error(resource) -> BaseException | None:
    """Attempt one close and return its error, if any."""
    try:
        resource.close()
    # silent-defaults: allow-exception-outcome -- Return the actual close failure to the owner for ordered reporting.
    except BaseException as error:
        return error
    return None


def _deadline(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise DockerError("invalid_policy", "Command timeout must be finite and between zero and 3600 seconds.")
    return time.monotonic() + timeout


def _remaining(deadline: float) -> float:
    result = deadline - time.monotonic()
    if result <= 0:
        raise DockerError("timeout", "The bounded Docker operation timed out.")
    return result


def _peek_exit(process: subprocess.Popen):
    """Observe exit without freeing the PID that pins our private process group."""
    try:
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError as error:
        raise DockerError("client_cleanup", "Docker child wait ownership was lost.") from error


def _reap(process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 1
    try:
        while True:
            pid, status = os.waitpid(process.pid, os.WNOHANG)
            if pid:
                process.returncode = os.waitstatus_to_exitcode(status)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DockerError("client_cleanup", "Docker client exit could not be confirmed.")
            time.sleep(min(0.01, remaining))
    except DockerError:
        raise
    except OSError as error:
        raise DockerError("client_cleanup", "Docker child could not be reaped.") from error
    except BaseException as error:
        raise _command_failure(error, DockerError("client_cleanup", "Docker child reaping was interrupted."),
                             mutation_uncertain=False) from error


def _terminate(process: subprocess.Popen) -> None:
    """Signal the owned group before reaping its leader, even after normal exit."""
    failure = None
    try:
        _peek_exit(process)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        # silent-defaults: allow-contract -- A vanished group needs no signal; its waitable leader must still be reaped.
        except ProcessLookupError:
            pass
    except DockerError:
        # Another waiter consumed the child: its numeric group may be reused.
        raise
    except OSError as error:
        failure = DockerError("client_cleanup", "Docker client group termination could not be confirmed.")
        failure.__cause__ = error
    except BaseException as error:
        failure = _command_failure(error, DockerError("client_cleanup", "Docker client retirement was interrupted."),
                                 mutation_uncertain=False)
    try:
        _reap(process)  # A failed signal must not skip reaping an already-exited child.
    except BaseException as error:
        if failure is not None:
            raise _command_failure(failure, error, mutation_uncertain=False) from failure
        raise
    if failure is not None:
        raise failure
