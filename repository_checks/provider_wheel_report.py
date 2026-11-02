"""Select locked wheels inside the target image with networking disabled.

The caller supplies the captured locks and a read-only pool mount, bounds the
worker's lifetime and temporary storage, and admits output only on success.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


LOCKS = ("requirements.lock", "dependencies/provider-core-api.lock", "dependencies/provider-core-source.txt")


def report(source: Path, pool: Path, expected: list[str]) -> bytes:
    if len(expected) != len(LOCKS) or any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in expected):
        raise ValueError("Provider wheel selection needs the three captured lock SHA-256 values.")
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary)
        for name, digest in zip(LOCKS, expected):
            content = (source / name).read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError(f"Provider lock changed before wheel selection: {name}")
            if name == LOCKS[2]:
                content, count = re.subn(rb"https://github\.com/numpde/nmr-provider-tools/releases/download/[^/]+/",
                                         pool.as_uri().encode() + b"/", content)
                if count != 1:
                    raise ValueError("Provider source lock must contain exactly one released SDK URL.")
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        result = snapshot / "report.json"
        directory = os.open(pool, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            fcntl.flock(directory, fcntl.LOCK_SH)
            subprocess.run(
                [sys.executable, "-I", "-m", "pip", "--isolated", "install", "--dry-run", "--ignore-installed",
                 "--no-index", "--no-deps", "--require-hashes", "--only-binary=:all:",
                 "--find-links", str(pool), "--report", str(result), "-r", str(snapshot / LOCKS[0])],
                check=True, stdout=sys.stderr, pass_fds=(directory,),
            )
            return result.read_bytes()
        finally:
            os.close(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--lock-sha256", action="append", required=True)
    arguments = parser.parse_args()
    sys.stdout.buffer.write(report(arguments.source, arguments.pool, arguments.lock_sha256))


if __name__ == "__main__":
    main()
