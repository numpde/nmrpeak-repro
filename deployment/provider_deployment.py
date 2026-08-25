"""Initialize one literal named deployment from committed examples."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys


_DEPLOYMENT_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_TEMPLATES = {
    "provider.toml": Path("config/provider.toml.example"),
    "deployment.toml": Path("config/deployment.toml.example"),
}


class DeploymentOperationRejected(RuntimeError):
    """A host deployment operation cannot prove its narrow write boundary."""


def initialize_deployment(repository: Path, deployment: str) -> Path:
    """Atomically publish editable config for one new literal deployment."""

    root = repository.resolve(strict=True)
    if root != repository or not root.is_dir():
        raise DeploymentOperationRejected(
            "Deployment repository must be one resolved directory"
        )
    _require_deployment_name(deployment)
    parent = root / "config/deployments"
    destination = parent / deployment
    if destination.exists() or destination.is_symlink():
        raise DeploymentOperationRejected(
            f"Deployment config already exists: {deployment}"
        )
    _require_clean_checkout(root)
    templates = {
        name: _read_committed_template(root, relative)
        for name, relative in _TEMPLATES.items()
    }
    _ensure_private_parent(parent)
    with _locked_parent(parent) as parent_fd:
        if _exists_at(parent_fd, deployment):
            raise DeploymentOperationRejected(
                f"Deployment config already exists: {deployment}"
            )
        stage_name = f".{deployment}.{secrets.token_hex(16)}.staging"
        os.mkdir(stage_name, mode=0o700, dir_fd=parent_fd)
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        written: list[str] = []
        try:
            for name, content in templates.items():
                _write_new_file(stage_fd, name, content)
                written.append(name)
            os.fsync(stage_fd)
            os.rename(
                stage_name,
                deployment,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        except BaseException:
            for name in written:
                os.unlink(name, dir_fd=stage_fd)
            os.close(stage_fd)
            stage_fd = -1
            os.rmdir(stage_name, dir_fd=parent_fd)
            raise
        finally:
            if stage_fd >= 0:
                os.close(stage_fd)
    return destination


def _require_deployment_name(deployment: str) -> None:
    if type(deployment) is not str or _DEPLOYMENT_NAME.fullmatch(deployment) is None:
        raise DeploymentOperationRejected(
            "Deployment name must contain only lowercase letters, digits, and interior hyphens"
        )


def _require_clean_checkout(repository: Path) -> None:
    result = subprocess.run(
        ("git", "-C", str(repository), "status", "--short"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise DeploymentOperationRejected(
            "Git could not inspect the deployment checkout"
        )
    if result.stdout:
        raise DeploymentOperationRejected(
            "Deployment initialization requires a clean committed checkout"
        )


def _read_committed_template(repository: Path, relative: Path) -> bytes:
    entry = subprocess.run(
        ("git", "-C", str(repository), "ls-tree", "HEAD", "--", str(relative)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    fields = entry.stdout.split(maxsplit=3)
    if entry.returncode != 0 or len(fields) != 4 or fields[:2] != ["100644", "blob"]:
        raise DeploymentOperationRejected(
            f"Deployment template is not a committed regular file: {relative}"
        )
    result = subprocess.run(
        ("git", "-C", str(repository), "show", f"HEAD:{relative.as_posix()}"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0 or not result.stdout:
        raise DeploymentOperationRejected(
            f"Deployment template is not a non-empty committed file: {relative}"
        )
    return result.stdout


def _ensure_private_parent(parent: Path) -> None:
    if parent.exists() or parent.is_symlink():
        if parent.is_symlink() or not parent.is_dir():
            raise DeploymentOperationRejected(
                "Deployment config parent must be a non-symlink directory"
            )
    else:
        try:
            parent.mkdir(mode=0o700)
            _fsync_directory(parent.parent)
        except FileExistsError:
            pass
    metadata = parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise DeploymentOperationRejected(
            "Deployment config parent must be operator-owned and not group/world writable"
        )


@contextmanager
def _locked_parent(parent: Path):
    descriptor = os.open(
        parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        os.close(descriptor)


def _exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _write_new_file(parent_fd: int, name: str, content: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operate one named NMRPeak deployment.")
    parser.add_argument("operation", choices=("init",))
    parser.add_argument("deployment")
    options = parser.parse_args(arguments)
    repository = Path(__file__).resolve().parents[1]
    try:
        initialized = initialize_deployment(repository, options.deployment)
    except (DeploymentOperationRejected, OSError) as error:
        print(f"Cannot initialize NMRPeak deployment: {error}", file=sys.stderr)
        return 2
    print(f"Initialized deployment {options.deployment}: {initialized}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
