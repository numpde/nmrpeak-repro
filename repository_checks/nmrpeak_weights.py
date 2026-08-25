"""Acquire and authenticate the pinned public NMRPeak weights archive."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import md5
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Callable, Sequence
from urllib.parse import parse_qs, urlsplit


DECLARATION_PATH = Path("families/nmrpeak/weights.declaration.json")
ARCHIVE_PATH = Path("weights/weights.zip")
PARTIAL_PATH = Path("weights/weights.zip.part")
LOCK_PATH = Path("weights/.download.lock")
_SCHEMA_ID = "nmrpeak-weights-acquisition-v1"
_DOI_PREFIX = "10.5281/zenodo."
_MD5 = re.compile(r"[0-9a-f]{32}")
_INTERFACE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_HASH_CHUNK_BYTES = 8 * 1024 * 1024


class WeightsAcquisitionRejected(ValueError):
    """The declared or materialized weights archive is not admissible."""


@dataclass(frozen=True, slots=True)
class WeightsDeclaration:
    doi: str
    url: str
    file_name: str
    byte_length: int
    md5: str


CurlRunner = Callable[[Sequence[str]], None]


def read_declaration(repository_root: Path) -> WeightsDeclaration:
    """Read one versioned Zenodo file identity from the tracked declaration."""

    path = repository_root / DECLARATION_PATH
    raw = _read_regular_file(path, "weights declaration")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WeightsAcquisitionRejected(
            "weights declaration is not valid UTF-8 JSON"
        ) from error
    if type(document) is not dict or set(document) != {
        "schema_id",
        "doi",
        "url",
        "file_name",
        "byte_length",
        "md5",
    }:
        raise WeightsAcquisitionRejected("weights declaration fields are not exact")
    if document["schema_id"] != _SCHEMA_ID:
        raise WeightsAcquisitionRejected("weights declaration schema is not supported")

    doi = document["doi"]
    url = document["url"]
    file_name = document["file_name"]
    byte_length = document["byte_length"]
    expected_md5 = document["md5"]
    if type(doi) is not str or not doi.startswith(_DOI_PREFIX):
        raise WeightsAcquisitionRejected("weights declaration DOI is invalid")
    record_id = doi.removeprefix(_DOI_PREFIX)
    if not record_id.isascii() or not record_id.isdecimal():
        raise WeightsAcquisitionRejected("weights declaration DOI is invalid")
    if type(file_name) is not str or file_name != "weights.zip":
        raise WeightsAcquisitionRejected("weights declaration file name is invalid")
    if type(byte_length) is not int or type(byte_length) is bool or byte_length <= 0:
        raise WeightsAcquisitionRejected("weights declaration byte length is invalid")
    if type(expected_md5) is not str or _MD5.fullmatch(expected_md5) is None:
        raise WeightsAcquisitionRejected("weights declaration MD5 is invalid")
    if type(url) is not str:
        raise WeightsAcquisitionRejected("weights declaration URL is invalid")
    parsed_url = urlsplit(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "zenodo.org"
        or parsed_url.path != f"/records/{record_id}/files/{file_name}"
        or parse_qs(parsed_url.query, strict_parsing=True) != {"download": ["1"]}
        or parsed_url.fragment
        or parsed_url.username
        or parsed_url.password
        or parsed_url.port is not None
    ):
        raise WeightsAcquisitionRejected(
            "weights declaration URL does not name the declared Zenodo record file"
        )
    return WeightsDeclaration(doi, url, file_name, byte_length, expected_md5)


def check_weights(repository_root: Path) -> None:
    """Prove the local archive equals the declared public Zenodo object."""

    declaration = read_declaration(repository_root)
    _verify_archive(repository_root / ARCHIVE_PATH, declaration)


def download_weights(
    repository_root: Path,
    interface: str | None,
    *,
    run_curl: CurlRunner | None = None,
) -> None:
    """Resume the declared object and publish it only after authentication."""

    if interface == "":
        interface = None
    root = repository_root.resolve(strict=True)
    declaration = read_declaration(root)
    archive = root / ARCHIVE_PATH
    partial = root / PARTIAL_PATH
    _prepare_archive_directory(archive.parent)

    with _acquisition_lock(root / LOCK_PATH):
        _download_weights_locked(declaration, archive, partial, interface, run_curl)


def _download_weights_locked(
    declaration: WeightsDeclaration,
    archive: Path,
    partial: Path,
    interface: str | None,
    run_curl: CurlRunner | None,
) -> None:
    """Own the complete check, resume, and publication transition."""

    if archive.exists() or archive.is_symlink():
        _verify_archive(archive, declaration)
        return
    _admit_partial(partial, declaration)
    curl = _curl_arguments(declaration, partial, interface)
    runner = run_curl or _run_curl
    try:
        runner(curl)
    except (OSError, subprocess.CalledProcessError) as error:
        raise WeightsAcquisitionRejected(
            "Zenodo weights download did not complete; the resumable partial was preserved"
        ) from error

    if archive.exists() or archive.is_symlink():
        raise WeightsAcquisitionRejected(
            "weights archive appeared while the download was in progress"
        )
    _verify_archive(partial, declaration)
    try:
        os.replace(partial, archive)
    except OSError as error:
        raise WeightsAcquisitionRejected(
            "authenticated weights could not be published; the partial was preserved"
        ) from error
    try:
        _fsync_directory(archive.parent)
    except OSError as error:
        raise WeightsAcquisitionRejected(
            "weights were published, but directory durability could not be confirmed"
        ) from error


@contextmanager
def _acquisition_lock(path: Path):
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise WeightsAcquisitionRejected(
            "weights acquisition lock is not accessible"
        ) from error
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid():
            raise WeightsAcquisitionRejected(
                "weights acquisition lock must be an operator-owned regular file"
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise WeightsAcquisitionRejected(
                "weights acquisition lock could not be held"
            ) from error
        yield
    finally:
        os.close(descriptor)


def _curl_arguments(
    declaration: WeightsDeclaration,
    partial: Path,
    interface: str | None,
) -> list[str]:
    if interface is not None and _INTERFACE.fullmatch(interface) is None:
        raise WeightsAcquisitionRejected("download network interface is invalid")
    arguments = [
        "curl",
        "--fail",
        "--location",
        "--continue-at",
        "-",
        "--retry",
        "5",
        "--retry-all-errors",
        "--retry-delay",
        "2",
        "--connect-timeout",
        "30",
    ]
    if interface is not None:
        arguments.extend(("--interface", interface))
    arguments.extend(("--output", os.fspath(partial), declaration.url))
    return arguments


def _run_curl(arguments: Sequence[str]) -> None:
    subprocess.run(arguments, check=True)


def _prepare_archive_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o775)
    except FileExistsError:
        pass
    try:
        mode = directory.lstat().st_mode
    except OSError as error:
        raise WeightsAcquisitionRejected(
            "weights archive directory is not accessible"
        ) from error
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise WeightsAcquisitionRejected(
            "weights archive directory must be a non-symlink directory"
        )


def _admit_partial(path: Path, declaration: WeightsDeclaration) -> None:
    if not path.exists() and not path.is_symlink():
        return
    size = _regular_file_size(path, "weights download partial")
    if size > declaration.byte_length:
        raise WeightsAcquisitionRejected(
            "weights download partial is larger than the declared archive"
        )


def _verify_archive(path: Path, declaration: WeightsDeclaration) -> None:
    size = _regular_file_size(path, "weights archive")
    if size != declaration.byte_length:
        raise WeightsAcquisitionRejected(
            f"weights archive has {size} bytes; expected {declaration.byte_length}"
        )
    digest = md5(usedforsecurity=False)
    try:
        with path.open("rb") as archive:
            while chunk := archive.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as error:
        raise WeightsAcquisitionRejected("weights archive is not readable") from error
    if digest.hexdigest() != declaration.md5:
        raise WeightsAcquisitionRejected(
            "weights archive MD5 differs from the checksum published by Zenodo"
        )


def _regular_file_size(path: Path, label: str) -> int:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise WeightsAcquisitionRejected(f"{label} is not accessible") from error
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise WeightsAcquisitionRejected(f"{label} must be a non-symlink regular file")
    return path.stat(follow_symlinks=False).st_size


def _read_regular_file(path: Path, label: str) -> bytes:
    _regular_file_size(path, label)
    try:
        return path.read_bytes()
    except OSError as error:
        raise WeightsAcquisitionRejected(f"{label} is not readable") from error


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("check", "download"))
    parser.add_argument("repository_root", type=Path)
    parser.add_argument("--interface")
    arguments = parser.parse_args()
    try:
        if arguments.operation == "check":
            if arguments.interface is not None:
                raise WeightsAcquisitionRejected(
                    "weights check does not accept a network interface"
                )
            check_weights(arguments.repository_root)
        else:
            download_weights(arguments.repository_root, arguments.interface)
    except WeightsAcquisitionRejected as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
