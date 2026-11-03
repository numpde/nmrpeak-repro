"""Run the bundled proxy transport without importing host site packages."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provider_core.bound_http_proxy import main

if __name__ == "__main__":
    raise SystemExit(main())
