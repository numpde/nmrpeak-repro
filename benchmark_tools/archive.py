"""Verify an archived experiment and reproduce its calculations without inference."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

from benchmark_tools.report import write_report

DERIVED = ("manifest.json", "measurements.jsonl", "outputs.jsonl", "summary.json", "comparison.md")


def verify_checksums(directory: Path) -> None:
    """Check the full published file inventory, including methods and raw logs."""
    entries = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, separator, name = line.partition("  ")
        path = Path(name)
        if (not separator or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or not name or path.is_absolute() or ".." in path.parts or path.as_posix() != name
                or name in entries or name == "SHA256SUMS"):
            raise ValueError(f"Invalid checksum entry in {directory / 'SHA256SUMS'}: {line!r}")
        entries[name] = digest
    paths = list(directory.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError(f"Cannot verify archive {directory}: symbolic links are not archived evidence")
    actual = {path.relative_to(directory).as_posix() for path in paths if path.is_file()} - {"SHA256SUMS"}
    if actual != set(entries):
        raise ValueError(f"Archive inventory differs: missing={sorted(set(entries) - actual)}, "
                         f"unlisted={sorted(actual - set(entries))}")
    for name, expected in entries.items():
        observed = sha256((directory / name).read_bytes()).hexdigest()
        if observed != expected:
            raise ValueError(f"Checksum mismatch for {name}: expected {expected}, observed {observed}")


def verify_archive(directory: Path) -> dict:
    verify_checksums(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    observed = "sha256:" + sha256((directory / "cases.json").read_bytes()).hexdigest()
    if observed != manifest["corpus_sha256"]:
        raise ValueError(f"Corpus identity differs: manifest={manifest['corpus_sha256']}, observed={observed}")
    for run in manifest["runs"]:
        if not run["id"] or Path(run["id"]).name != run["id"] or run["id"] in {".", ".."}:
            raise ValueError(f"Cannot read worker log for invalid run ID {run['id']!r}")
    # The existing reporter rewrites its manifest. Regenerate only in scratch space.
    with tempfile.TemporaryDirectory(prefix="nmrpeak-report-") as temporary:
        scratch = Path(temporary)
        shutil.copytree(directory / "logs", scratch / "logs")
        summary = write_report(scratch, manifest)
        if summary["status"] != "complete":
            raise ValueError("Archived experiment is incomplete; inspect run statuses and raw worker events")
        for name in DERIVED:
            if (scratch / name).read_bytes() != (directory / name).read_bytes():
                raise ValueError(f"Recomputed {name} differs from the archived file; inspect raw events and reporter revision")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", help="published benchmark directory containing SHA256SUMS")
    args = parser.parse_args()
    if not args.run:
        parser.error("set RUN=benchmark/<timestamp>-<sha> to select an archived experiment")
    try:
        summary = verify_archive(Path(args.run))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"Cannot verify benchmark archive: {error}\n")
    print(f"Verified {args.run}: file checksums and all derived reports match; "
          f"{sum(row['samples'] for row in summary['latencies'])} measured observations. "
          "Inference and chemistry validation were not rerun.")


if __name__ == "__main__":
    main()
