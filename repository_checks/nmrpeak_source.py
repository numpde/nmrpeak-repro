"""Authenticate the NMRPeak source closure used by runner images."""

from __future__ import annotations

import argparse
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys


SOURCE_DECLARATION = Path("families/nmrpeak/source-closure.paths")
SOURCE_MANIFEST = Path("families/nmrpeak/source-closure.sha256")
UPSTREAM = Path("nmrpeak-upstream")


def verify_nmrpeak_source(repository_root: Path) -> None:
    """Prove committed and live source bytes equal the declared closure."""

    root = Path(repository_root)
    source_revision, closure_roots = _read_declaration(root / SOURCE_DECLARATION)
    expected_hashes = _read_manifest(root / SOURCE_MANIFEST)
    committed_paths = _committed_paths(root / UPSTREAM, source_revision, closure_roots)
    if set(expected_hashes) != set(committed_paths):
        raise ValueError("NMRPeak source manifest does not match the committed closure")

    for path in committed_paths:
        committed = _git(
            root / UPSTREAM,
            "cat-file",
            "blob",
            f"{source_revision}:{path}",
        )
        if sha256(committed).hexdigest() != expected_hashes[path]:
            raise ValueError(f"NMRPeak committed source content drift: {path}")

    head = _git(root / UPSTREAM, "rev-parse", "HEAD").decode("ascii").strip()
    if head != source_revision:
        raise ValueError("NMRPeak submodule is not at the declared source revision")

    _verify_materialized_source(root / UPSTREAM, closure_roots, expected_hashes)


def verify_materialized_nmrpeak_source(
    source_root: Path,
    declaration_path: Path,
    manifest_path: Path,
) -> None:
    """Prove a Docker build's materialized source has no other bytes."""

    _revision, closure_roots = _read_declaration(declaration_path)
    expected_hashes = _read_manifest(manifest_path)
    _verify_materialized_source(Path(source_root), closure_roots, expected_hashes)


def read_nmrpeak_source_revision(declaration_path: Path) -> str:
    """Read the revision from the authenticated source-closure declaration."""

    revision, _roots = _read_declaration(declaration_path)
    return revision


def _read_declaration(path: Path) -> tuple[str, tuple[str, ...]]:
    lines = _read_regular_file(path).decode("ascii", errors="strict").splitlines()
    if not lines or not lines[0].startswith("source_revision "):
        raise ValueError("NMRPeak source declaration has no revision")
    revision = lines[0].removeprefix("source_revision ")
    valid_revision = len(revision) == 40 and all(
        character in "0123456789abcdef" for character in revision
    )
    if not valid_revision:
        raise ValueError("NMRPeak source declaration revision is invalid")
    roots = tuple(lines[1:])
    if not roots or len(set(roots)) != len(roots):
        raise ValueError("NMRPeak source declaration roots are invalid")
    for root in roots:
        normalized = PurePosixPath(root)
        if (
            not root
            or root == "."
            or normalized.is_absolute()
            or normalized.as_posix() != root
            or ".." in normalized.parts
        ):
            raise ValueError("NMRPeak source declaration contains an invalid root")
    for index, root in enumerate(roots):
        root_path = PurePosixPath(root)
        for other in roots[index + 1 :]:
            other_path = PurePosixPath(other)
            if root_path in other_path.parents or other_path in root_path.parents:
                raise ValueError("NMRPeak source declaration roots overlap")
    return revision, roots


def _read_manifest(path: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    raw = _read_regular_file(path).decode("ascii", errors="strict")
    for line in raw.splitlines():
        digest, separator, relative_path = line.partition("  ")
        normalized = PurePosixPath(relative_path)
        if (
            separator != "  "
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not relative_path
            or normalized.as_posix() != relative_path
            or ".." in normalized.parts
            or relative_path in hashes
        ):
            raise ValueError("NMRPeak source manifest is malformed")
        hashes[relative_path] = digest
    if not hashes:
        raise ValueError("NMRPeak source manifest is empty")
    return hashes


def _committed_paths(
    upstream: Path,
    revision: str,
    roots: tuple[str, ...],
) -> tuple[str, ...]:
    output = _git(
        upstream,
        "ls-tree",
        "-r",
        "-z",
        revision,
        "--",
        *roots,
    )
    paths: list[str] = []
    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", 1)
        mode, object_type, _object_id = metadata.decode("ascii").split(" ")
        if mode not in {"100644", "100755"} or object_type != "blob":
            raise ValueError("NMRPeak committed source contains a special file")
        paths.append(raw_path.decode("utf-8", errors="strict"))
    return tuple(paths)


def _live_paths(upstream: Path, roots: tuple[str, ...]) -> tuple[str, ...]:
    if upstream.is_symlink() or not upstream.is_dir():
        raise ValueError("NMRPeak live source root is not a directory")
    paths: list[str] = []
    for root in roots:
        candidate = upstream / root
        if candidate.is_symlink():
            raise ValueError("NMRPeak live source contains a symlink")
        if candidate.is_file():
            paths.append(root)
            continue
        if not candidate.is_dir():
            raise ValueError("NMRPeak live source root is missing")
        for directory, directory_names, file_names in os.walk(candidate):
            directory_names.sort()
            file_names.sort()
            directory_path = Path(directory)
            for name in directory_names:
                if (directory_path / name).is_symlink():
                    raise ValueError("NMRPeak live source contains a symlink")
            for name in file_names:
                path = directory_path / name
                if path.is_symlink():
                    raise ValueError("NMRPeak live source contains a symlink")
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError("NMRPeak live source contains a special file")
                paths.append(path.relative_to(upstream).as_posix())
    return tuple(paths)


def _verify_materialized_source(
    source_root: Path,
    closure_roots: tuple[str, ...],
    expected_hashes: dict[str, str],
) -> None:
    live_paths = _live_paths(source_root, closure_roots)
    if set(live_paths) != set(expected_hashes):
        raise ValueError("NMRPeak live source inventory does not match the closure")
    for path in live_paths:
        digest = sha256(_read_regular_file(source_root / path)).hexdigest()
        if digest != expected_hashes[path]:
            raise ValueError(f"NMRPeak live source content drift: {path}")


def _read_regular_file(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("NMRPeak source input is not a readable regular file") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("NMRPeak live source contains a special file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _git(repository: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise ValueError("cannot inspect the pinned NMRPeak source") from error


def _main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", nargs="?", type=Path)
    parser.add_argument("--materialized", type=Path)
    parser.add_argument("--declaration", type=Path)
    parser.add_argument("--manifest", type=Path)
    options = parser.parse_args(arguments)
    materialized_arguments = (
        options.materialized,
        options.declaration,
        options.manifest,
    )
    if options.repository is not None and not any(materialized_arguments):
        verify_nmrpeak_source(options.repository)
        return
    if options.repository is None and all(materialized_arguments):
        verify_materialized_nmrpeak_source(*materialized_arguments)
        return
    parser.error("choose REPOSITORY or all three materialized-source arguments")


if __name__ == "__main__":
    _main(sys.argv[1:])
