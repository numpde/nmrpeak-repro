"""Run unittest discovery while rejecting an empty proof lane."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import unittest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("start_directory", type=Path)
    parser.add_argument("--top-level", type=Path, required=True)
    parser.add_argument("--pattern", default="test_*.py")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(args.start_directory),
        pattern=args.pattern,
        top_level_dir=str(args.top_level),
    )
    if suite.countTestCases() == 0:
        print(f"no tests discovered under {args.start_directory}", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
