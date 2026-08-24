#!/usr/bin/env python3
"""Validate and extract the downloaded weights ZIP inside a container only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile


MAX_ENTRIES = 10_000
MAX_UNCOMPRESSED_BYTES = 40 * 1024**3
COPY_CHUNK_BYTES = 8 * 1024**2


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"refusing to extract: {message}")


def relative_weight_path(info: zipfile.ZipInfo) -> Path | None:
    name = info.filename
    if "\\" in name or "\x00" in name:
        fail(f"invalid member name: {name!r}")

    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        fail(f"member escapes extraction root: {name!r}")
    if not path.parts or path.parts[0] == "__MACOSX":
        return None
    if path.parts[0] != "weights":
        fail(f"unexpected top-level member: {name!r}")

    mode = info.external_attr >> 16
    if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        fail(f"non-regular member: {name!r}")

    relative_parts = path.parts[1:]
    return Path(*relative_parts) if relative_parts else Path(".")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--member",
        action="append",
        default=[],
        help="exact archive member to extract; repeat to allow more than one",
    )
    args = parser.parse_args()

    archive = args.archive
    destination = args.destination
    staging = destination / ".staging"
    current = destination / "current"

    if current.is_dir() and (current / ".extracted.json").is_file():
        print("weights are already extracted")
        return
    if current.exists() or staging.exists():
        fail("destination contains an incomplete extraction; recreate the weights volume")

    with zipfile.ZipFile(archive) as bundle:
        all_members = bundle.infolist()
        if args.member:
            requested = set(args.member)
            by_name = {member.filename: member for member in all_members}
            missing = sorted(requested - by_name.keys())
            if missing:
                fail(f"requested members are absent: {missing}")
            members = [by_name[name] for name in args.member]
        else:
            members = all_members
        if len(members) > MAX_ENTRIES:
            fail(f"archive has {len(members)} entries (limit: {MAX_ENTRIES})")

        total_size = sum(member.file_size for member in members)
        if total_size > MAX_UNCOMPRESSED_BYTES:
            fail(
                f"archive expands to {total_size} bytes "
                f"(limit: {MAX_UNCOMPRESSED_BYTES})"
            )

        planned = [(member, relative_weight_path(member)) for member in members]
        staging.mkdir(mode=0o755)

        files_written = 0
        for member, relative_path in planned:
            if relative_path is None or relative_path == Path("."):
                continue
            target = staging / relative_path
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=COPY_CHUNK_BYTES)
            os.chmod(target, 0o444)
            files_written += 1

    marker = staging / ".extracted.json"
    marker.write_text(
        json.dumps(
            {
                "archive_bytes": archive.stat().st_size,
                "files": files_written,
                "members": args.member or ["weights/**"],
                "uncompressed_bytes": total_size,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(marker, 0o444)
    staging.rename(current)
    print(f"extracted {files_written} files ({total_size} bytes) into {current}")


if __name__ == "__main__":
    main()
