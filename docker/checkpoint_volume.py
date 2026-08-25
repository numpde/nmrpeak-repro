#!/usr/bin/env python3
"""Populate or inspect one NMRPeak checkpoint volume without loading it."""

from __future__ import annotations

import argparse
import base64
from hashlib import sha256
import os
from pathlib import Path
import stat
import sys


VOLUME_PATH = Path("/volume")
CHECKPOINT_PATH = VOLUME_PATH / "checkpoint.pt"
MARKER_PATH = VOLUME_PATH / ".nmrpeak-checkpoint.json"
COPY_CHUNK_BYTES = 8 * 1024 * 1024


class CheckpointVolumeRejected(RuntimeError):
    """The mounted volume cannot become or prove an admitted checkpoint."""


def populate(checkpoint_bytes: int, checkpoint_sha256: str, marker: bytes) -> None:
    """Write, remeasure, and admit one stdin checkpoint into an empty volume."""

    _require_empty_volume()
    descriptor = _create_regular_file(CHECKPOINT_PATH)
    digest = sha256()
    count = 0
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as destination:
            while chunk := sys.stdin.buffer.read(COPY_CHUNK_BYTES):
                count += len(chunk)
                if count > checkpoint_bytes:
                    raise CheckpointVolumeRejected(
                        "checkpoint input exceeds its declared byte length"
                    )
                digest.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        CHECKPOINT_PATH.unlink(missing_ok=True)
        raise
    if count != checkpoint_bytes or "sha256:" + digest.hexdigest() != checkpoint_sha256:
        CHECKPOINT_PATH.unlink(missing_ok=True)
        raise CheckpointVolumeRejected(
            "checkpoint input differs from its release declaration"
        )
    os.chmod(CHECKPOINT_PATH, 0o444, follow_symlinks=False)
    _verify_checkpoint(checkpoint_bytes, checkpoint_sha256)
    _write_marker(marker)
    _verify_admitted_inventory(checkpoint_bytes, checkpoint_sha256, marker)
    _fsync_volume()


def verify(checkpoint_bytes: int, checkpoint_sha256: str, marker: bytes) -> None:
    """Prove the exact admitted two-file volume inventory and bytes."""

    _verify_admitted_inventory(checkpoint_bytes, checkpoint_sha256, marker)


def verify_recoverable() -> None:
    """Prove residue has no admission marker or foreign volume entry."""

    names = _volume_names()
    if MARKER_PATH.name in names:
        raise CheckpointVolumeRejected(
            "an admitted checkpoint volume cannot be recovered as incomplete"
        )
    if not names.issubset({CHECKPOINT_PATH.name}):
        raise CheckpointVolumeRejected(
            "incomplete checkpoint volume contains an unexpected entry"
        )
    if CHECKPOINT_PATH.name in names:
        metadata = CHECKPOINT_PATH.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise CheckpointVolumeRejected(
                "incomplete checkpoint path is not a regular file"
            )


def _require_empty_volume() -> None:
    if _volume_names():
        raise CheckpointVolumeRejected("new checkpoint volume is not empty")


def _volume_names() -> set[str]:
    try:
        return {entry.name for entry in VOLUME_PATH.iterdir()}
    except OSError as error:
        raise CheckpointVolumeRejected("checkpoint volume is not readable") from error


def _create_regular_file(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags, 0o400)
    except OSError as error:
        raise CheckpointVolumeRejected(
            "checkpoint volume refused a new regular file"
        ) from error


def _write_marker(marker: bytes) -> None:
    descriptor = _create_regular_file(MARKER_PATH)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as destination:
            destination.write(marker)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(MARKER_PATH, 0o444, follow_symlinks=False)
    except BaseException:
        MARKER_PATH.unlink(missing_ok=True)
        raise


def _verify_admitted_inventory(
    checkpoint_bytes: int,
    checkpoint_sha256: str,
    marker: bytes,
) -> None:
    if _volume_names() != {CHECKPOINT_PATH.name, MARKER_PATH.name}:
        raise CheckpointVolumeRejected(
            "checkpoint volume does not have its exact admitted inventory"
        )
    _verify_checkpoint(checkpoint_bytes, checkpoint_sha256)
    _verify_regular_read_only_file(MARKER_PATH)
    if MARKER_PATH.read_bytes() != marker:
        raise CheckpointVolumeRejected(
            "checkpoint admission marker differs from the volume identity"
        )


def _verify_checkpoint(checkpoint_bytes: int, checkpoint_sha256: str) -> None:
    _verify_regular_read_only_file(CHECKPOINT_PATH)
    digest = sha256()
    count = 0
    with CHECKPOINT_PATH.open("rb") as source:
        while chunk := source.read(COPY_CHUNK_BYTES):
            count += len(chunk)
            digest.update(chunk)
    if count != checkpoint_bytes or "sha256:" + digest.hexdigest() != checkpoint_sha256:
        raise CheckpointVolumeRejected(
            "checkpoint volume bytes differ from the release declaration"
        )


def _verify_regular_read_only_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CheckpointVolumeRejected(
            "checkpoint volume is missing an admitted regular file"
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o444
    ):
        raise CheckpointVolumeRejected(
            "checkpoint volume entry is not a mode-0444 regular file"
        )


def _fsync_volume() -> None:
    descriptor = os.open(VOLUME_PATH, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _decode_marker(value: str) -> bytes:
    try:
        marker = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise CheckpointVolumeRejected(
            "checkpoint admission marker argument is not base64"
        ) from error
    if not marker or len(marker) > 4096:
        raise CheckpointVolumeRejected(
            "checkpoint admission marker has an invalid byte length"
        )
    return marker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("populate", "verify", "recoverable"))
    parser.add_argument("--checkpoint-bytes", type=int)
    parser.add_argument("--checkpoint-sha256")
    parser.add_argument("--marker-base64")
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "recoverable":
            if any(
                value is not None
                for value in (
                    arguments.checkpoint_bytes,
                    arguments.checkpoint_sha256,
                    arguments.marker_base64,
                )
            ):
                parser.error("recoverable does not accept checkpoint facts")
            verify_recoverable()
            return 0
        if (
            arguments.checkpoint_bytes is None
            or arguments.checkpoint_bytes <= 0
            or arguments.checkpoint_sha256 is None
            or arguments.marker_base64 is None
        ):
            parser.error(
                "populate and verify require checkpoint bytes, digest, and marker"
            )
        marker = _decode_marker(arguments.marker_base64)
        if arguments.operation == "populate":
            populate(
                arguments.checkpoint_bytes,
                arguments.checkpoint_sha256,
                marker,
            )
        else:
            verify(
                arguments.checkpoint_bytes,
                arguments.checkpoint_sha256,
                marker,
            )
    except (CheckpointVolumeRejected, OSError) as error:
        parser.exit(2, f"Checkpoint volume rejected: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
