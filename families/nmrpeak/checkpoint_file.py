"""Keep one NMRPeak checkpoint descriptor across verification and model load."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO


CHECKPOINT_PATH = Path("/checkpoint/checkpoint.pt")
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
_HASH_CHUNK_BYTES = 1024 * 1024


def open_verified_checkpoint(expected_checkpoint_ref: str) -> BinaryIO:
    """Open, verify, and rewind the fixed checkpoint for deserialization."""

    return _open_verified_checkpoint(CHECKPOINT_PATH, expected_checkpoint_ref)


def _open_verified_checkpoint(
    checkpoint_path: Path,
    expected_checkpoint_ref: str,
) -> BinaryIO:
    if (
        type(expected_checkpoint_ref) is not str
        or _SHA256_REF.fullmatch(expected_checkpoint_ref) is None
    ):
        raise ValueError("expected NMRPeak checkpoint identity is not a SHA-256 reference")

    descriptor = os.open(
        checkpoint_path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("NMRPeak checkpoint is not a regular file")
        checkpoint = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise

    try:
        measured = sha256()
        while chunk := checkpoint.read(_HASH_CHUNK_BYTES):
            measured.update(chunk)
        if "sha256:" + measured.hexdigest() != expected_checkpoint_ref:
            raise RuntimeError("NMRPeak checkpoint bytes differ from the admitted release")
        checkpoint.seek(0)
        return checkpoint
    except BaseException:
        checkpoint.close()
        raise
