"""Project one immutable NMR API provider contract release from Git objects."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory

from nmrpeak_provider.provider_http_contract import (
    PROVIDER_HTTP_RELEASE_REF,
    load_provider_http_contract_release,
)


_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_FILES = 64
_RELEASE_REF = re.compile(r"sha256:[0-9a-f]{64}")


class ProjectionRejected(RuntimeError):
    """The selected Git release cannot safely become the local projection."""


def check_projection(
    repository: Path,
    nmr_api_v1: Path,
    release_ref: str,
) -> None:
    """Require the committed projection to equal one authenticated Git release."""

    projected = _project_release(nmr_api_v1, release_ref)
    destination = repository / "contracts/upstream/nmr_api_v1"
    _require_projection_bytes(destination, projected)


def write_projection(
    repository: Path,
    nmr_api_v1: Path,
    release_ref: str,
) -> None:
    """Atomically replace only the fixed provider contract projection."""

    projected = _project_release(nmr_api_v1, release_ref)
    destination = repository / "contracts/upstream/nmr_api_v1"
    try:
        _require_projection_bytes(destination, projected)
        return
    except ProjectionRejected:
        pass
    parent = destination.parent
    _require_owned_directory(parent)
    stage = parent / f".nmr_api_v1.{secrets.token_hex(16)}.staging"
    predecessor = parent / f".nmr_api_v1.{secrets.token_hex(16)}.previous"
    try:
        _write_projection_tree(stage, projected)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    moved_predecessor = False
    published = False
    try:
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ProjectionRejected(
                    "NMR API contract projection is not a replaceable directory"
                )
            os.rename(destination, predecessor)
            moved_predecessor = True
        os.rename(stage, destination)
        published = True
        _fsync_directory(parent)
    except BaseException:
        if published:
            os.rename(destination, stage)
        if moved_predecessor:
            os.rename(predecessor, destination)
        _fsync_directory(parent)
        shutil.rmtree(stage, ignore_errors=True)
        raise
    if moved_predecessor:
        try:
            shutil.rmtree(predecessor)
        except OSError as error:
            raise ProjectionRejected(
                "Updated NMR API projection is installed, but its predecessor remains"
            ) from error
        _fsync_directory(parent)


def _project_release(nmr_api_v1: Path, release_ref: str) -> dict[str, bytes]:
    if release_ref != PROVIDER_HTTP_RELEASE_REF or _RELEASE_REF.fullmatch(
        release_ref
    ) is None:
        raise ProjectionRejected(
            "NMR API release must equal the provider contract consumed by this revision"
        )
    checkout = nmr_api_v1.resolve(strict=True)
    if checkout != nmr_api_v1 or not checkout.is_dir():
        raise ProjectionRejected("NMR API checkout must be one resolved directory")
    revision = _git(checkout, "rev-parse", "--verify", "HEAD^{commit}").decode(
        "ascii"
    ).strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ProjectionRejected("NMR API checkout returned an invalid Git revision")
    digest = release_ref.removeprefix("sha256:")
    prefix = f"nmr_api/provider/contract_releases/v1/{digest}/"
    listed = _git(
        checkout,
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        revision,
        "--",
        prefix,
    ).split(b"\0")
    if listed[-1:] == [b""]:
        listed.pop()
    if not listed or len(listed) > _MAX_FILES:
        raise ProjectionRejected("NMR API release has an invalid file count")
    projected: dict[str, bytes] = {}
    for encoded in listed:
        try:
            source_path = encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProjectionRejected(
                "NMR API release contains a non-UTF-8 path"
            ) from error
        if not source_path.startswith(prefix):
            raise ProjectionRejected("NMR API release escaped its Git tree")
        relative = source_path.removeprefix(prefix)
        if (
            not relative
            or PurePosixPath(relative).as_posix() != relative
            or ".." in PurePosixPath(relative).parts
            or relative in projected
        ):
            raise ProjectionRejected("NMR API release contains an invalid path")
        size_raw = _git(checkout, "cat-file", "-s", f"{revision}:{source_path}")
        try:
            size = int(size_raw)
        except ValueError as error:
            raise ProjectionRejected(
                "NMR API release artifact size is invalid"
            ) from error
        if size < 1 or size > _MAX_ARTIFACT_BYTES:
            raise ProjectionRejected(
                "NMR API release artifact exceeds its byte bound"
            )
        content = _git(checkout, "show", f"{revision}:{source_path}")
        if len(content) != size:
            raise ProjectionRejected("NMR API release artifact size changed")
        projected[relative] = content
    with TemporaryDirectory() as temporary:
        root = Path(temporary) / "nmr_api_v1"
        _write_projection_tree(root, projected)
        try:
            load_provider_http_contract_release(root)
        except (OSError, TypeError, ValueError) as error:
            raise ProjectionRejected(
                "NMR API Git release is not the authenticated provider contract"
            ) from error
    return projected


def _require_projection_bytes(root: Path, expected: dict[str, bytes]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ProjectionRejected("NMR API contract projection is unavailable")
    actual: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ProjectionRejected(
                "NMR API contract projection contains an unsupported path"
            )
        if path.is_file():
            if path.stat(follow_symlinks=False).st_size > _MAX_ARTIFACT_BYTES:
                raise ProjectionRejected(
                    "NMR API contract projection artifact exceeds its byte bound"
                )
            actual[path.relative_to(root).as_posix()] = path.read_bytes()
    if actual != expected:
        raise ProjectionRejected(
            "Committed NMR API contract projection differs from the selected release"
        )
    try:
        load_provider_http_contract_release(root)
    except (OSError, TypeError, ValueError) as error:
        raise ProjectionRejected(
            "Committed NMR API contract projection is invalid"
        ) from error


def _write_projection_tree(root: Path, projected: dict[str, bytes]) -> None:
    root.mkdir(mode=0o755)
    for relative, content in sorted(projected.items()):
        destination = root / relative
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o644,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        _fsync_directory(directory)
    _fsync_directory(root)


def _require_owned_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise ProjectionRejected(
            "NMR API projection parent must be an operator-owned directory"
        )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _git(repository: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProjectionRejected("NMR API Git object read did not complete") from error
    if result.returncode != 0 or len(result.stdout) > _MAX_ARTIFACT_BYTES:
        raise ProjectionRejected("NMR API Git object read was rejected")
    return result.stdout


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Project one NMR API contract release.")
    parser.add_argument("operation", choices=("check", "write"))
    parser.add_argument("repository", type=Path)
    parser.add_argument("nmr_api_v1", type=Path)
    parser.add_argument("release_ref")
    options = parser.parse_args(arguments)
    try:
        if options.operation == "check":
            check_projection(options.repository, options.nmr_api_v1, options.release_ref)
        else:
            write_projection(options.repository, options.nmr_api_v1, options.release_ref)
    except (OSError, ProjectionRejected) as error:
        print(f"Cannot {options.operation} the NMR API projection: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
