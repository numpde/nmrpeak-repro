"""Start owner mapping from the verified source bundle under isolated Python."""

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    # -I excludes the script directory; the caller has verified this bundle.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    runpy.run_module("acquisition_tool.identity_main", run_name="__main__", alter_sys=False)
