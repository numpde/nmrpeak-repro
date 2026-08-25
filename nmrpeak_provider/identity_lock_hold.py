"""Hold one admitted provider identity lock until the caller closes stdin."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .provider_identity_lock import ProviderIdentityLock


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lock_path", type=Path)
    parser.add_argument("provider_ref")
    options = parser.parse_args(arguments)
    with ProviderIdentityLock.acquire(options.lock_path, options.provider_ref):
        sys.stdout.buffer.write(b"READY\n")
        sys.stdout.buffer.flush()
        if sys.stdin.buffer.read(1):
            raise RuntimeError("Provider identity-lock holder received input")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
