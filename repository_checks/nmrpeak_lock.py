from __future__ import annotations

import argparse
from pathlib import Path
import re
import stat
import tomllib
from urllib.parse import urldefrag, parse_qs


MAX_LOCK_BYTES = 16 * 1024 * 1024
PYPI_INDEX = "--index-url https://pypi.org/simple"
REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<constraint>==[^ ]+| @ https://[^ ]+)$"
)
HASH = re.compile(r"^    --hash=sha256:(?P<digest>[0-9a-f]{64})$")
PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^ ]+)$")


class LockRejected(ValueError):
    pass


def normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def require_regular_file(path: Path, operation: str) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise LockRejected(f"Cannot {operation}; the lock file does not exist: {path}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise LockRejected(f"Cannot {operation}; the lock is not a regular non-symlink file: {path}")
    if metadata.st_size == 0 or metadata.st_size > MAX_LOCK_BYTES:
        raise LockRejected(
            f"Cannot {operation}; lock size {metadata.st_size} is outside 1..{MAX_LOCK_BYTES} bytes: {path}"
        )
    return path.read_bytes()


def direct_requirements(intent: Path, target: str) -> dict[str, tuple[str, str | None]]:
    document = tomllib.loads(intent.read_text(encoding="utf-8"))
    try:
        declared = document["project"]["dependencies"] + document["dependency-groups"][target]
    except KeyError as error:
        raise LockRejected(
            f"Cannot check the {target} lock; family intent lacks the required {error.args[0]!r} declaration."
        ) from error
    requirements: dict[str, tuple[str, str | None]] = {}
    for declaration in declared:
        if " @ " in declaration:
            name, source = declaration.split(" @ ", 1)
            url, fragment = urldefrag(source)
            hashes = parse_qs(fragment, strict_parsing=True)
            digest = hashes.get("sha256", [None])
            if set(hashes) != {"sha256"} or len(digest) != 1 or not re.fullmatch(
                r"[0-9a-f]{64}", digest[0] or ""
            ):
                raise LockRejected(
                    f"Cannot check the {target} lock; direct URL requirement lacks one SHA-256 digest: {name}"
                )
            constraint = f" @ {url}"
            expected_hash = digest[0]
        else:
            match = PIN.fullmatch(declaration)
            if match is None:
                raise LockRejected(
                    f"Cannot check the {target} lock; direct requirement is not an exact pin: {declaration}"
                )
            name = match["name"]
            constraint = f"=={match['version']}"
            expected_hash = None
        key = normalized_name(name)
        if key in requirements:
            raise LockRejected(
                f"Cannot check the {target} lock; direct requirement is declared more than once: {name}"
            )
        requirements[key] = (constraint, expected_hash)
    return requirements


def parse_locked_requirements(lines: list[str], operation: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    requirements: dict[str, tuple[str, tuple[str, ...]]] = {}
    index = 4
    while index < len(lines):
        line = lines[index]
        if not line or line.startswith("    #"):
            index += 1
            continue
        if not line.endswith(" \\"):
            raise LockRejected(
                f"Cannot {operation}; requirement line {index + 1} does not continue into artifact hashes."
            )
        match = REQUIREMENT.fullmatch(line[:-2])
        if match is None:
            raise LockRejected(
                f"Cannot {operation}; line {index + 1} is not a pinned requirement or generated annotation."
            )
        name = normalized_name(match["name"])
        if name in requirements:
            raise LockRejected(f"Cannot {operation}; requirement is listed more than once: {name}")
        index += 1
        hash_lines: list[tuple[str, bool]] = []
        while index < len(lines) and lines[index].startswith("    --hash="):
            continued = lines[index].endswith(" \\")
            hash_text = lines[index][:-2] if continued else lines[index]
            hash_match = HASH.fullmatch(hash_text)
            if hash_match is None:
                raise LockRejected(
                    f"Cannot {operation}; line {index + 1} is not a canonical SHA-256 artifact hash."
                )
            hash_lines.append((hash_match["digest"], continued))
            index += 1
        if not hash_lines:
            raise LockRejected(f"Cannot {operation}; requirement has no admitted SHA-256 artifact: {name}")
        if any(not continued for _, continued in hash_lines[:-1]) or hash_lines[-1][1]:
            raise LockRejected(f"Cannot {operation}; requirement hash continuations are not canonical: {name}")
        hashes = [digest for digest, _ in hash_lines]
        if hashes != sorted(set(hashes)):
            raise LockRejected(f"Cannot {operation}; requirement hashes are duplicate or unsorted: {name}")
        requirements[name] = (match["constraint"], tuple(hashes))
    if list(requirements) != sorted(requirements):
        raise LockRejected(f"Cannot {operation}; requirements are not in canonical name order.")
    return requirements


def check_lock(repo_root: Path, target: str, lock: Path) -> None:
    operation = f"check the {target} lock"
    content = require_regular_file(lock, operation)
    if not content.endswith(b"\n") or b"\r" in content:
        raise LockRejected(f"Cannot {operation}; the lock must use canonical LF-terminated text: {lock}")
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise LockRejected(f"Cannot {operation}; the lock is not UTF-8 text: {lock}") from error
    expected_header = [
        "# This file was autogenerated by uv via the following command:",
        f"#    make runner/lock/stage TARGET={target}",
        PYPI_INDEX,
        "",
    ]
    if lines[:4] != expected_header:
        raise LockRejected(
            f"Cannot {operation}; the generated header does not name the canonical stage command and PyPI index."
        )
    locked = parse_locked_requirements(lines, operation)
    expected = direct_requirements(repo_root / "families/nmrpeak/pyproject.toml", target)
    for name, (constraint, expected_hash) in expected.items():
        locked_requirement = locked.get(name)
        if locked_requirement is None:
            raise LockRejected(
                f"Cannot {operation}; direct requirement does not match family intent: {name}{constraint}"
            )
        locked_constraint, locked_hashes = locked_requirement
        if expected_hash is None:
            constraint_matches = locked_constraint == constraint
        else:
            locked_url, locked_fragment = urldefrag(locked_constraint.removeprefix(" @ "))
            fragment_hashes = parse_qs(locked_fragment, strict_parsing=True) if locked_fragment else {}
            constraint_matches = (
                locked_constraint.startswith(" @ ")
                and f" @ {locked_url}" == constraint
                and (not fragment_hashes or fragment_hashes == {"sha256": [expected_hash]})
            )
        if not constraint_matches:
            raise LockRejected(
                f"Cannot {operation}; direct requirement does not match family intent: {name}{constraint}"
            )
        if expected_hash is not None and expected_hash not in locked_hashes:
            raise LockRejected(
                f"Cannot {operation}; direct URL digest does not match family intent: {name}"
            )
    unexpected_urls = [
        name
        for name, (constraint, _) in locked.items()
        if constraint.startswith(" @ ") and name not in expected
    ]
    if unexpected_urls:
        raise LockRejected(
            f"Cannot {operation}; transitive requirements use unreviewed direct URLs: {', '.join(unexpected_urls)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate one admitted NMRPeak family dependency lock.")
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("target", choices=("cpu-x86_64",))
    parser.add_argument("lock", type=Path)
    args = parser.parse_args()
    try:
        check_lock(args.repo_root.resolve(strict=True), args.target, args.lock)
    except (LockRejected, OSError, tomllib.TOMLDecodeError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
