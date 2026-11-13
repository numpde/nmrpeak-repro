"""Project identities through the verified shared selected-engine owner."""

from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from tempfile import mkdtemp


_OWNER = None
_SNAPSHOT = None


class IdentityProbeError(RuntimeError):
    """The selected engine's identity could not be established."""


class IdentityProbeUnsettled(IdentityProbeError):
    """The probe or its native client may still be present."""


def _shared_owner():
    global _OWNER, _SNAPSHOT
    bundle = (Path(__file__).resolve().parents[1] / "bootstrap/acquisition").resolve()
    for name, module in tuple(sys.modules.items()):
        if name in ("acquisition_tool", "linux_idmap") or name.startswith(("acquisition_tool.", "linux_idmap.")):
            if _OWNER is None:
                raise IdentityProbeError("Cannot map engine identity: acquisition or linux-idmap was loaded before bundle admission")
            filename = getattr(module, "__file__", None)
            expected = bundle / name.split(".")[0]
            if not filename or not Path(filename).resolve().is_relative_to(expected):
                raise IdentityProbeError("Cannot map engine identity: an ambient acquisition or linux-idmap module is loaded")
    sys.path.insert(0, str(bundle))
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        updater = importlib.import_module("acquisition_tool.source_update")
        if Path(updater.__file__).resolve() != bundle / "acquisition_tool/source_update.py":
            raise IdentityProbeError("Cannot map engine identity: source verifier is not from the pinned bundle")
        try:
            manifest = updater.verify(bundle)
        except (OSError, ValueError) as error:
            raise IdentityProbeError(f"Cannot map engine identity: acquisition source verification failed: {error}") from error
        snapshot = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        if _SNAPSHOT is not None and snapshot != _SNAPSHOT:
            raise IdentityProbeError("Cannot map engine identity: verified acquisition bundle changed during this process")
        owner = importlib.import_module("acquisition_tool.identity_main")
        if Path(owner.__file__).resolve() != bundle / "acquisition_tool/identity_main.py":
            raise IdentityProbeError("Cannot map engine identity: identity owner is not from the pinned bundle")
        _OWNER, _SNAPSHOT = owner, snapshot
        return owner
    finally:
        sys.dont_write_bytecode = previous_bytecode
        sys.path.remove(str(bundle))


def _retired(receipt: Path) -> bool:
    try:
        descriptor = os.open(receipt, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        metadata = os.fstat(descriptor)
        return (stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid()
                and os.read(descriptor, 9) == b"retired\n")
    except OSError:
        return False
    finally:
        os.close(descriptor)


def project_uids(endpoint: str, image: str, uids: tuple[int, ...], *, inverse: bool = False) -> tuple[int, ...]:
    owner = _shared_owner()
    evidence = Path(mkdtemp(prefix="nmrpeak-identity-"))
    receipt = evidence / "retired"
    arguments = [
        "--endpoint", endpoint, "--image", image, "--interpreter", "/usr/local/bin/python3.12",
        "--user-uid", "65532", "--user-gid", "65532", "--memory-bytes", "33554432",
        "--pids-limit", "4", "--nano-cpus", "250000000", "--retired-receipt", str(receipt),
    ]
    for uid in uids:
        arguments.extend(("--map", "uid", "inverse" if inverse else "forward", str(uid)))
    stdout, stderr = io.StringIO(), io.StringIO()
    error = None
    status = None
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = owner.main(arguments)
    except BaseException as cause:
        error = cause
    if not _retired(receipt):
        raise IdentityProbeUnsettled(
            f"Cannot map engine identity: probe retirement is unconfirmed; status {status!r}; "
            f"retain {evidence}; {stderr.getvalue().strip()}"
        ) from error
    cleanup_error = None
    try:
        shutil.rmtree(evidence)
    except OSError as cause:
        cleanup_error = cause
    if error is not None:
        if cleanup_error is not None:
            diagnostic = f"Identity evidence cleanup failed at {evidence}: {cleanup_error}"
            if isinstance(error, SystemExit):
                print(diagnostic, file=sys.stderr)
            error.add_note(diagnostic)
        raise error
    if type(status) is not int:
        invalid = IdentityProbeError("Cannot map engine identity: identity owner returned an invalid status")
        if cleanup_error is not None:
            invalid.add_note(f"Identity evidence cleanup failed at {evidence}: {cleanup_error}")
        raise invalid
    if status in (129, 130, 143):
        cancellation = SystemExit(status)
        if cleanup_error is not None:
            diagnostic = f"Identity evidence cleanup failed at {evidence}: {cleanup_error}"
            print(diagnostic, file=sys.stderr)
            cancellation.add_note(diagnostic)
        raise cancellation
    if cleanup_error is not None:
        raise IdentityProbeError(
            f"Cannot remove retired identity evidence at {evidence}: {cleanup_error}; "
            f"identity status {status}; {stderr.getvalue().strip()}"
        ) from cleanup_error
    if status != 0:
        raise IdentityProbeError(f"Cannot map engine identity (status {status}): {stderr.getvalue().strip()}")
    output = stdout.getvalue()
    if re.fullmatch(r"[0-9]+(?: [0-9]+)*\n", output) is None:
        raise IdentityProbeError("Cannot map engine identity: projection output is malformed")
    values = tuple(int(value) for value in output.split())
    if len(values) != len(uids) or any(value >= 2**32 - 1 for value in values):
        raise IdentityProbeError("Cannot map engine identity: projection output is incomplete")
    return values
