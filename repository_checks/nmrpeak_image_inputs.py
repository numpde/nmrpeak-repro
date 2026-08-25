"""Materialize one exact committed NMRPeak image context."""

from __future__ import annotations

import argparse
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys

from repository_checks.nmrpeak_source import (
    read_source_declaration,
    read_source_manifest,
)


SOURCE_CLOSURES = (
    (
        Path("nmrpeak-upstream"),
        Path("families/nmrpeak/source-closure.paths"),
        Path("families/nmrpeak/source-closure.sha256"),
    ),
    (
        Path("unicore-upstream"),
        Path("families/nmrpeak/unicore-closure.paths"),
        Path("families/nmrpeak/unicore-closure.sha256"),
    ),
)


class ImageInputRejected(ValueError):
    pass


def materialize_image_context(
    repo_root: Path,
    revision: str,
    destination: Path,
    runner: str,
) -> str:
    """Write and identify the exact committed inputs admitted by one image."""

    _require_revision(revision)
    _require_empty_directory(destination)
    image_root = _image_root(runner)
    declaration_path = image_root / "image-inputs.txt"
    declaration = _git_file(repo_root, revision, declaration_path)[1]
    selected_paths = _read_selected_paths(declaration, image_root, runner)
    if runner == "provider":
        _require_complete_provider_inputs(repo_root, revision, selected_paths)
    for path in selected_paths:
        mode, content = _git_file(repo_root, revision, path)
        _write_file(destination / path, content, mode)

    if runner != "provider":
        _materialize_source_closures(repo_root, revision, destination, runner)

    return "sha256:" + _context_digest(destination)


def _materialize_source_closures(
    repo_root: Path,
    revision: str,
    destination: Path,
    runner: str,
) -> None:
    for upstream, declaration_path, manifest_path in SOURCE_CLOSURES:
        source_revision, closure_roots = read_source_declaration(destination / declaration_path)
        expected_hashes = read_source_manifest(destination / manifest_path)
        gitlink_mode, gitlink_revision = _git_entry(repo_root, revision, upstream)
        if gitlink_mode != "160000" or gitlink_revision != source_revision:
            raise ImageInputRejected(
                f"Cannot materialize the {runner} image context; {upstream} does not match its declared revision."
            )
        upstream_repository = repo_root / upstream
        committed_paths = _git_tree_paths(
            upstream_repository,
            source_revision,
            closure_roots,
        )
        if set(committed_paths) != set(expected_hashes):
            raise ImageInputRejected(
                f"Cannot materialize the {runner} image context; {upstream} manifest does not own its complete source closure."
            )
        for path in sorted(expected_hashes):
            mode, content = _git_file(
                upstream_repository,
                source_revision,
                Path(path),
            )
            if sha256(content).hexdigest() != expected_hashes[path]:
                raise ImageInputRejected(
                    f"Cannot materialize the {runner} image context; committed source digest differs: {upstream / path}"
                )
            _write_file(destination / upstream / path, content, mode)


def _require_complete_provider_inputs(
    repo_root: Path,
    revision: str,
    selected_paths: tuple[Path, ...],
) -> None:
    package_paths = {
        Path(path)
        for path in _git_tree_paths(repo_root, revision, ("nmrpeak_provider",))
    }
    required_paths = package_paths | {
        Path("containers/provider/Dockerfile"),
        Path("containers/provider/Dockerfile.dockerignore"),
        Path("pyproject.toml"),
        Path("requirements.lock"),
    }
    if set(selected_paths) != required_paths:
        raise ImageInputRejected(
            "Provider image inputs must select exactly its Docker policy, package, metadata, and dependency lock."
        )


def _image_root(runner: str) -> Path:
    if runner == "provider":
        return Path("containers/provider")
    if runner == "nmrpeak_chf_v1":
        return Path("models/nmrpeak_chf_v1/runner")
    if runner == "nmrpeak_hf_v1":
        return Path("models/nmrpeak_hf_v1/runner")
    raise ImageInputRejected(f"NMRPeak image is not supported: {runner or '<unset>'}")


def _read_selected_paths(raw: bytes, runner_root: Path, runner: str) -> tuple[Path, ...]:
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as error:
        raise ImageInputRejected(f"{runner} image input declaration is not UTF-8 text.") from error
    if not lines or lines != sorted(set(lines)):
        raise ImageInputRejected(f"{runner} image input paths must be non-empty, sorted, and unique.")
    paths: list[Path] = []
    for line in lines:
        path = PurePosixPath(line)
        if (
            not line
            or path.is_absolute()
            or path.as_posix() != line
            or ".." in path.parts
            or any(ord(character) < 32 for character in line)
        ):
            raise ImageInputRejected(f"{runner} image input path is unsafe: {line!r}")
        if any(path == upstream or upstream in path.parents for upstream, _, _ in SOURCE_CLOSURES):
            raise ImageInputRejected(
                f"{runner} image inputs must select pinned source through its closure manifest: {line}"
            )
        paths.append(Path(line))
    dockerfile_name = "Dockerfile" if runner == "provider" else "Dockerfile.runner"
    required = {runner_root / dockerfile_name, runner_root / f"{dockerfile_name}.dockerignore"}
    if not required.issubset(paths):
        raise ImageInputRejected(
            f"{runner} image inputs omit the Dockerfile or its deny-by-default ignore file."
        )
    return tuple(paths)


def _git_file(repository: Path, revision: str, path: Path) -> tuple[int, bytes]:
    mode, object_id = _git_entry(repository, revision, path)
    if mode not in {"100644", "100755"}:
        raise ImageInputRejected(f"NMRPeak image input is not a regular committed file: {path}")
    content = _git(repository, "cat-file", "blob", object_id)
    return (0o755 if mode == "100755" else 0o644), content


def _git_entry(repository: Path, revision: str, path: Path) -> tuple[str, str]:
    raw = _git(repository, "ls-tree", "-z", revision, "--", path.as_posix())
    entries = [entry for entry in raw.split(b"\0") if entry]
    if len(entries) != 1:
        raise ImageInputRejected(f"NMRPeak image input is not one committed Git entry: {path}")
    metadata, raw_path = entries[0].split(b"\t", 1)
    mode, object_type, object_id = metadata.decode("ascii").split(" ")
    if raw_path.decode("utf-8", errors="strict") != path.as_posix():
        raise ImageInputRejected(f"Git returned a different NMRPeak image input path: {path}")
    expected_type = "commit" if mode == "160000" else "blob"
    if object_type != expected_type:
        raise ImageInputRejected(f"NMRPeak image input has an unsupported Git object type: {path}")
    return mode, object_id


def _git_tree_paths(
    repository: Path,
    revision: str,
    closure_roots: tuple[str, ...],
) -> tuple[str, ...]:
    raw = _git(repository, "ls-tree", "-r", "-z", revision, "--", *closure_roots)
    paths: list[str] = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, object_type, _object_id = metadata.decode("ascii").split(" ")
        if mode not in {"100644", "100755"} or object_type != "blob":
            raise ImageInputRejected("Pinned source closure contains a special Git entry.")
        paths.append(raw_path.decode("utf-8", errors="strict"))
    return tuple(paths)


def _write_file(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _context_digest(root: Path) -> str:
    identity = sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if not candidate.is_dir()):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ImageInputRejected(f"NMRPeak image context contains a special file: {path.relative_to(root)}")
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        executable = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
        identity.update(f"{executable} {sha256(content).hexdigest()} {len(content)} {relative}\n".encode("utf-8"))
    return identity.hexdigest()


def _require_empty_directory(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise ImageInputRejected(f"NMRPeak image context destination is not a non-symlink directory: {path}")
    if any(path.iterdir()):
        raise ImageInputRejected(f"NMRPeak image context destination is not empty: {path}")


def _require_revision(revision: str) -> None:
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ImageInputRejected(f"NMRPeak image source revision is not one full Git object ID: {revision}")


def _git(repository: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise ImageInputRejected("Cannot read one committed NMRPeak image input from Git.") from error


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Materialize a committed NMRPeak image context.")
    parser.add_argument("runner")
    parser.add_argument("repository", type=Path)
    parser.add_argument("revision")
    parser.add_argument("destination", type=Path)
    options = parser.parse_args(arguments)
    try:
        print(
            materialize_image_context(
                options.repository.resolve(strict=True),
                options.revision,
                options.destination.resolve(strict=True),
                options.runner,
            )
        )
    except (ImageInputRejected, OSError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main(sys.argv[1:])
