"""Verify and explicitly replace a consumer's checked-in acquisition source.

Only standard-library code runs before package acquisition. The old, reviewed
updater checks the old snapshot and authenticates incoming archive bytes before
reading candidate metadata. Builds never call the network update implicitly.
Source maintenance requires quiescent readers; a directory exchange cannot
retire processes that already hold source files open.
"""

import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile


def _json(data, description):
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as error:
        error.add_note(f"Reading {description}.")
        raise


def _version(value):
    return type(value) is str and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)


def _checksum(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value)


def read_pin(path):
    """Read the consumer's expected release; downloads never rewrite this pin."""
    data, _metadata = _regular_bytes(Path(path))
    pin = _json(data, f"release pin {path}")
    if (type(pin) is not dict or set(pin) != {"version", "archive_sha256"}
            or not _version(pin["version"]) or not _checksum(pin["archive_sha256"])):
        raise ValueError(f"Cannot read release pin {path}: require version and archive_sha256.")
    return pin


def _relative_name(name):
    if type(name) is not str or not name or "\0" in name:
        raise ValueError("Acquisition inventory contains an empty or non-text file path.")
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise ValueError(f"Acquisition inventory path is not a canonical relative file: {name!r}.")
    return path


def read_manifest(data):
    """Admit the recorded inventory, excluding only the manifest's own bytes."""
    manifest = _json(data, "acquisition-manifest.json")
    if type(manifest) is not dict or set(manifest) != {
        "release_version", "source_revision", "payload_files", "dependency_inputs",
    }:
        raise ValueError("Acquisition manifest must identify its release, source, payload and dependency inputs.")
    if not _version(manifest["release_version"]) or not (
        type(manifest["source_revision"]) is str
        and re.fullmatch(r"[0-9a-f]{40}", manifest["source_revision"])
    ):
        raise ValueError("Acquisition manifest has no exact release version or Git source revision.")
    entries = manifest["payload_files"]
    if type(entries) is not dict or not entries or "acquisition-manifest.json" in entries:
        raise ValueError("Acquisition manifest must inventory its payload without hashing itself.")
    for name, entry in entries.items():
        path = _relative_name(name)
        if any(parent.as_posix() in entries for parent in path.parents):
            raise ValueError(f"Acquisition payload file conflicts with a parent path: {name!r}.")
        if not (type(entry) is dict and set(entry) == {"sha256", "size", "mode"}
                and _checksum(entry["sha256"]) and type(entry["size"]) is int and 0 <= entry["size"]
                and type(entry["mode"]) is int and entry["mode"] in (0o644, 0o755)):
            raise ValueError(f"Acquisition payload has invalid file metadata: {name!r}.")
    dependencies = manifest["dependency_inputs"]
    if type(dependencies) is not dict or any(
        type(name) is not str or not name or not _checksum(checksum)
        for name, checksum in dependencies.items()
    ):
        raise ValueError("Acquisition dependency inputs must map artifact names to SHA-256 checksums.")
    return manifest


def _regular_bytes(path):
    """Read one opened regular file, without following links or blocking on a FIFO."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Cannot read acquisition input {path}: require a regular file.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(), metadata
    finally:
        os.close(descriptor)


def _check_bytes(name, data, entry):
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError(f"Acquisition file differs from its recorded release: {name}.")


def verify(source_dir):
    """Check installed files against their OLD manifest and return that manifest.

    This deliberately takes no incoming pin: a maintainer can select the next
    release before updating the old snapshot. Git review remains the trust
    boundary for coordinated changes to metadata, payload or this updater.
    """
    source = Path(source_dir).absolute()
    if source.resolve() != source or source.is_symlink() or not source.is_dir():
        raise ValueError(f"Cannot verify acquisition source {source}: require a real directory without symlink parents.")
    data, metadata = _regular_bytes(source / "acquisition-manifest.json")
    if metadata.st_mode & 0o111 or metadata.st_nlink != 1:
        raise ValueError("The acquisition manifest must not be executable or have extra hard links.")
    manifest = read_manifest(data)
    entries = manifest["payload_files"]
    expected_dirs = {parent.as_posix() for name in entries
                     for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")}
    actual = set()
    for root, directories, files in os.walk(source, followlinks=False):
        for name in directories:
            directory = Path(root) / name
            if directory.is_symlink() or directory.relative_to(source).as_posix() not in expected_dirs:
                raise ValueError(f"Acquisition source contains an unrecorded or symlink directory: {directory}.")
        for name in files:
            path = Path(root) / name
            relative = path.relative_to(source).as_posix()
            if relative == "acquisition-manifest.json":
                actual.add(relative)
                continue
            if relative not in entries:
                raise ValueError(f"Acquisition source contains an unrecorded file: {relative}.")
            content, metadata = _regular_bytes(path)
            if metadata.st_nlink != 1:
                raise ValueError(f"Acquisition source file has extra hard links: {relative}.")
            _check_bytes(relative, content, entries[relative])
            # Git records executable bits, not checkout read/write permissions.
            if (metadata.st_mode & 0o111) != (entries[relative]["mode"] & 0o111):
                raise ValueError(f"Acquisition file executable bits differ from its recorded release: {relative}.")
            actual.add(relative)
    if actual != set(entries) | {"acquisition-manifest.json"}:
        raise ValueError("Acquisition source is missing files from its recorded release.")
    return manifest


def _admit_archive(data, pin):
    # The manifest is inside the download. It cannot authenticate that download.
    observed = hashlib.sha256(data).hexdigest()
    if observed != pin["archive_sha256"]:
        raise ValueError(f"Acquisition archive checksum mismatch: expected {pin['archive_sha256']}, received {observed}.")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(set(names)) != len(names):
            raise ValueError("Acquisition archive contains duplicate members.")
        for member in members:
            path = _relative_name(member.name)
            if any(parent.as_posix() in names for parent in path.parents):
                raise ValueError(f"Acquisition archive member conflicts with its parent: {member.name!r}.")
            if not member.isfile():
                raise ValueError(f"Acquisition archive contains a link or special member: {member.name!r}.")
        if names.count("acquisition-manifest.json") != 1:
            raise ValueError("Acquisition archive must contain one acquisition-manifest.json.")
        manifest_member = archive.getmember("acquisition-manifest.json")
        if manifest_member.mode != 0o644:
            raise ValueError("Acquisition archive manifest must have mode 0644.")
        payload = {member.name: archive.extractfile(member).read() for member in members}
        manifest = read_manifest(payload["acquisition-manifest.json"])
        if manifest["release_version"] != pin["version"]:
            raise ValueError("Acquisition archive version differs from the reviewed release pin.")
        entries = manifest["payload_files"]
        if set(payload) != set(entries) | {"acquisition-manifest.json"}:
            raise ValueError("Acquisition archive members differ from its embedded inventory.")
        for member in members:
            if member.name == "acquisition-manifest.json":
                continue
            entry = entries[member.name]
            if member.size != entry["size"] or member.mode != entry["mode"]:
                raise ValueError(f"Acquisition archive member metadata differs: {member.name}.")
            _check_bytes(member.name, payload[member.name], entry)
    return payload, manifest


def _exchange_directories(first, second):
    """Use the existing Linux directory-exchange primitive from SECS bootstrap."""
    libc = ctypes.CDLL(None, use_errno=True)
    exchange = libc.renameat2
    exchange.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    exchange.restype = ctypes.c_int
    if exchange(-100, os.fsencode(first), -100, os.fsencode(second), 2) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), f"{first} <-> {second}")


def _publish(source, stage):
    # Everything is verified before this single publication operation. Code
    # and its manifest change together; the consumer's pin is never rewritten.
    try:
        _exchange_directories(source, stage)
    except BaseException as error:
        error.add_note(f"Snapshot exchange did not complete normally; retain and inspect {source} and {stage}.")
        raise
    try:
        shutil.rmtree(stage)
    except BaseException as error:
        error.add_note(f"New acquisition source is published at {source}; previous snapshot cleanup failed at {stage}.")
        raise


def _import_verified_old(source, pin, archive):
    data, _metadata = _regular_bytes(Path(archive))
    payload, manifest = _admit_archive(data, pin)
    stage = Path(tempfile.mkdtemp(prefix=".acquisition-next-", dir=source.parent))
    try:
        manifest_path = stage / "acquisition-manifest.json"
        with manifest_path.open("xb") as stream:
            stream.write(payload["acquisition-manifest.json"])
        manifest_path.chmod(0o644)
        for name, entry in manifest["payload_files"].items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(payload[name])
            path.chmod(entry["mode"])
        for root, _directories, _files in os.walk(stage):
            Path(root).chmod(0o755)
        verify(stage)
        # A download or staging can take time. Refuse an old-source edit made
        # while this command was preparing the replacement, before exchanging.
        verify(source)
    except BaseException as primary:
        try:
            shutil.rmtree(stage)
        except OSError as cleanup:
            raise BaseExceptionGroup("Acquisition staging failed and its cleanup also failed", [primary, cleanup])
        raise
    _publish(source, stage)
    return manifest


def import_archive(source_dir, pin_file, archive):
    """Replace clean old source from an authenticated local archive, offline."""
    source = Path(source_dir).absolute()
    verify(source)
    return _import_verified_old(source, read_pin(pin_file), archive)


def update(source_dir, pin_file):
    """Download the one pinned GitHub asset and use the same import boundary."""
    source = Path(source_dir).absolute()
    verify(source)
    pin = read_pin(pin_file)
    name = f"acquisition-owner-{pin['version']}.tar.gz"
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix="provider-tools-release-") as temporary:
            subprocess.run([
                "gh", "release", "download", f"v{pin['version']}",
                "--repo", "numpde/nmr-provider-tools", "--dir", temporary, "--pattern", name,
            ], check=True)
            manifest = _import_verified_old(source, pin, Path(temporary) / name)
            published = True
    except BaseException as error:
        if published:
            error.add_note(f"Acquisition source was published at {source}; release-download cleanup failed.")
        raise
    return manifest


def main():
    """Expose source verification, pinned remote update and offline import."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("verify", "update", "import"):
        command = commands.add_parser(operation)
        command.add_argument("--source-dir", type=Path, required=True)
        if operation != "verify":
            command.add_argument("--pin-file", type=Path, required=True)
        if operation == "import":
            command.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    if args.operation == "verify":
        verify(args.source_dir)
    elif args.operation == "update":
        update(args.source_dir, args.pin_file)
    else:
        import_archive(args.source_dir, args.pin_file, args.archive)


if __name__ == "__main__":
    main()
