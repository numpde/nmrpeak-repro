"""Materialize one self-contained, ignored-file-free test source snapshot."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess


class SnapshotRejected(RuntimeError):
    pass


SENSITIVE_ROOTS = frozenset((".agents", ".aws", ".codex", "config/deployments", "secrets", "weights"))
ALLOWED_SUBMODULES = frozenset(("nmrpeak-upstream", "unicore-upstream"))


def _git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotRejected(f"git {' '.join(arguments[:2])} failed: {diagnostic}")
    return result.stdout


def _listed_files(repository: Path) -> tuple[str, ...]:
    raw = _git(repository, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    try:
        paths = tuple(item.decode("utf-8") for item in raw.split(b"\0") if item)
    except UnicodeDecodeError as error:
        raise SnapshotRejected("test source contains a non-UTF-8 path") from error
    for item in paths:
        path = Path(item)
        if path.is_absolute() or ".." in path.parts or item in {"", "."}:
            raise SnapshotRejected(f"unsafe Git path: {item!r}")
    return paths


def _gitlinks(repository: Path, revision: str) -> tuple[str, ...]:
    raw = _git(repository, "ls-tree", "-r", "-z", revision)
    paths = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, _object_id = metadata.split(b" ", 2)
        if mode != b"160000" and object_type != b"commit":
            continue
        if mode != b"160000" or object_type != b"commit":
            raise SnapshotRejected("malformed Git submodule tree entry")
        try:
            value = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SnapshotRejected("submodule path is not UTF-8") from error
        paths.append(value)
    return tuple(sorted(paths))


def _submodules(repository: Path, revision: str) -> tuple[str, ...]:
    paths = _gitlinks(repository, revision)
    for value in paths:
        if value not in ALLOWED_SUBMODULES:
            raise SnapshotRejected(f"unreviewed submodule path: {value!r}")
    return paths


def _entry_bytes(root: Path, relative: str) -> tuple[str, int, bytes] | None:
    path = root / relative
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    resolved_parent = path.parent.resolve(strict=True)
    if resolved_parent != path.parent.absolute():
        raise SnapshotRejected(f"source path has a symlinked parent: {relative}")
    try:
        resolved_parent.relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise SnapshotRejected(f"source path escapes through a parent symlink: {relative}") from error
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISREG(metadata.st_mode):
        return "file", mode, path.read_bytes()
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink", mode, os.readlink(path).encode("utf-8")
    if stat.S_ISDIR(metadata.st_mode):
        return None
    raise SnapshotRejected(f"test source is not a regular file or symlink: {relative}")


def _inventory(repository: Path, *, excluded: frozenset[str] = frozenset()) -> tuple[tuple[str, str, int, bytes], ...]:
    entries = []
    for relative in _listed_files(repository):
        if any(relative == item or relative.startswith(item + "/") for item in excluded):
            continue
        value = _entry_bytes(repository, relative)
        if value is not None:
            entries.append((relative, value[0], value[1], value[2]))
    return tuple(entries)


def _reject_tracked_sensitive_roots(repository: Path, label: str, revision: str) -> None:
    raw = (
        _git(repository, "ls-files", "-z", "--cached")
        + _git(repository, "ls-tree", "-r", "-z", "--name-only", revision)
    )
    try:
        tracked = {item.decode("utf-8") for item in raw.split(b"\0") if item}
    except UnicodeDecodeError as error:
        raise SnapshotRejected("test source contains a non-UTF-8 path") from error
    exposed = sorted(
        relative for relative in tracked
        if any(relative == root or relative.startswith(root + "/") for root in SENSITIVE_ROOTS)
    )
    if exposed:
        raise SnapshotRejected(
            f"tracked sensitive path in {label}: {exposed[0]!r}"
        )


def _publish_inventory(source: Path, destination: Path, inventory: tuple[tuple[str, str, int, bytes], ...]) -> None:
    del source
    for relative, kind, mode, payload in inventory:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "symlink":
            target.symlink_to(payload.decode("utf-8"))
        else:
            target.write_bytes(payload)
            target.chmod(mode)


def _clone_metadata(source: Path, destination: Path, revision: str | None = None) -> None:
    if revision is None:
        revision = _git(source, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    destination.mkdir(parents=True)
    _git(destination, "init", "--quiet")
    result = subprocess.run(
        (
            "git", "-c", "protocol.file.allow=always", "-C", str(destination),
            "fetch", "--quiet", "--depth=1", "--no-tags", "--no-write-fetch-head",
            "--", source.as_uri(), revision,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise SnapshotRejected(
            "minimal local Git metadata fetch failed: "
            + result.stderr.decode("utf-8", errors="replace").strip()
        )
    _git(destination, "update-ref", "--no-deref", "HEAD", revision)
    _git(destination, "read-tree", "HEAD")


def materialize(source: Path, destination: Path) -> str:
    source = source.resolve(strict=True)
    if destination.exists():
        raise SnapshotRejected(f"snapshot destination already exists: {destination}")
    super_revision = _git(source, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    submodules = _submodules(source, super_revision)
    _reject_tracked_sensitive_roots(source, ".", super_revision)
    super_inventory = _inventory(source, excluded=frozenset(submodules) | SENSITIVE_ROOTS)
    _clone_metadata(source, destination, super_revision)
    _publish_inventory(source, destination, super_inventory)

    inventories = [("", source, destination, super_inventory, super_revision.encode("ascii"))]
    for relative in submodules:
        module_source = source / relative
        revision = _git(module_source, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        nested = _gitlinks(module_source, revision)
        if nested:
            raise SnapshotRejected(f"nested submodule path: {relative}/{nested[0]}")
        _reject_tracked_sensitive_roots(module_source, relative, revision)
        module_inventory = _inventory(module_source, excluded=SENSITIVE_ROOTS)
        _clone_metadata(module_source, destination / relative, revision)
        _publish_inventory(module_source, destination / relative, module_inventory)
        inventories.append(
            (relative, module_source, destination / relative, module_inventory, revision.encode("ascii"))
        )

    digest = hashlib.sha256()
    for prefix, repository, snapshot_repository, before, revision in inventories:
        after = _inventory(
            repository,
            excluded=(frozenset(submodules) | SENSITIVE_ROOTS) if not prefix else SENSITIVE_ROOTS,
        )
        if before != after:
            raise SnapshotRejected(f"test source changed while snapshotting: {prefix or '.'}")
        if _git(repository, "rev-parse", "--verify", "HEAD").strip() != revision:
            raise SnapshotRejected(f"Git revision changed while snapshotting: {prefix or '.'}")
        if _git(snapshot_repository, "rev-parse", "--verify", "HEAD").strip() != revision:
            raise SnapshotRejected(f"snapshot Git revision is wrong: {prefix or '.'}")
        digest.update(prefix.encode("utf-8") + b"\0" + revision + b"\0")
        for relative, kind, mode, payload in before:
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update(kind.encode("ascii") + b"\0" + f"{mode:o}".encode("ascii") + b"\0")
            digest.update(hashlib.sha256(payload).digest())
    return "sha256:" + digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        print(materialize(args.source, args.destination))
    except (OSError, SnapshotRejected) as error:
        print(f"test snapshot rejected: {error}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
