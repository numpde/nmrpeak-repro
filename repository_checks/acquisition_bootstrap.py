"""Verify the complete acquisition bootstrap against the SDK release record."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "dependencies/provider-core-release.json"
PIN = ROOT / "dependencies/provider-core-source.txt"


BUNDLE = ROOT / 'bootstrap/acquisition'



def pinned_wheel(record):
    """Require the bootstrap issuer record to name the runtime's exact wheel."""
    match = re.fullmatch(
        r'provider-core @ https://github\.com/numpde/nmr-provider-tools/releases/download/v'
        r'([0-9]+\.[0-9]+\.[0-9]+)/provider_core-\1-py3-none-any\.whl \\\n'
        r'    --hash=sha256:([0-9a-f]{64})\n?', PIN.read_text(),
    )
    if match is None:
        raise ValueError('SDK source pin does not name one exact released wheel')
    wheel_name = f'provider_core-{match[1]}-py3-none-any.whl'
    if record['files'].get(wheel_name) != match[2]:
        raise ValueError('SDK bootstrap release record differs from the runtime wheel pin')
    return wheel_name, match[2]



def verify(bundle: Path, record: dict) -> None:
    inventory = record['acquisition_source_files']
    if not isinstance(inventory, dict) or not inventory:
        raise ValueError('Acquisition bootstrap release inventory is empty.')
    expected = set()
    for name in inventory:
        path = PurePosixPath(name)
        if not isinstance(name, str) or path.is_absolute() or '..' in path.parts or path.as_posix() != name:
            raise ValueError('Acquisition bootstrap release inventory contains an invalid path.')
        expected.add(name)
    actual = set()
    for current, directories, files in os.walk(bundle, followlinks=False):
        parent = Path(current)
        for name in directories:
            if (parent / name).is_symlink():
                raise ValueError('Acquisition bootstrap contains a symlink directory.')
        for name in files:
            path = parent / name
            relative = path.relative_to(bundle).as_posix()
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError(f'Acquisition bootstrap member is not a regular file: {relative}')
            actual.add(relative)
    if bundle.is_symlink() or not bundle.is_dir() or actual != expected:
        raise ValueError('Acquisition bootstrap members differ from the release inventory.')
    for name, entry in inventory.items():
        path = bundle / name
        data = path.read_bytes()
        if (len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']
                or (stat.S_IMODE(path.stat().st_mode) & 0o111) != (entry['mode'] & 0o111)):
            raise ValueError(f'Acquisition bootstrap member differs from the release record: {name}')


def refresh(bundle: Path, record: dict, archive_path: Path, version: str) -> None:
    name = f'acquisition-owner-{version}.tar.gz'
    data = archive_path.read_bytes()
    if archive_path.name != name or hashlib.sha256(data).hexdigest() != record['files'][name]:
        raise ValueError('Acquisition bootstrap archive differs from the release record.')
    inventory = record['acquisition_source_files']
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = archive.getmembers()
        if len(members) != len(inventory) or {m.name for m in members} != set(inventory):
            raise ValueError('Acquisition bootstrap archive members differ from the release inventory.')
        contents = {}
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or path.as_posix() != member.name or not member.isfile():
                raise ValueError('Acquisition bootstrap archive contains an unsupported member.')
            stream = archive.extractfile(member)
            assert stream is not None
            content = stream.read()
            entry = inventory[member.name]
            if len(content) != entry['size'] or hashlib.sha256(content).hexdigest() != entry['sha256'] or member.mode != entry['mode']:
                raise ValueError(f'Acquisition bootstrap archive member differs from the release record: {member.name}')
            contents[member.name] = content
    if bundle.parent.is_symlink():
        raise ValueError('Acquisition bootstrap parent must not be a symlink.')
    bundle.parent.mkdir(parents=True, exist_ok=True)
    if not bundle.parent.is_dir():
        raise ValueError('Acquisition bootstrap parent must be a directory.')
    if bundle.is_symlink() or (bundle.exists() and not bundle.is_dir()):
        raise ValueError('Acquisition bootstrap destination must be a non-symlink directory.')
    stage = Path(tempfile.mkdtemp(prefix='.acquisition-', dir=bundle.parent))
    backup = None
    try:
        for name, content in contents.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(inventory[name]['mode'])
        verify(stage, record)
        if bundle.exists():
            backup = Path(tempfile.mkdtemp(prefix='.acquisition-previous-', dir=bundle.parent))
            backup.rmdir()
            bundle.rename(backup)
        try:
            stage.rename(bundle)
        except BaseException:
            if backup is not None:
                backup.rename(bundle)
                backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='install this exact issued acquisition source bundle')
    args = parser.parse_args()
    record = json.loads(RECORD.read_text())
    wheel_name, _ = pinned_wheel(record)
    version = wheel_name.removeprefix('provider_core-').removesuffix('-py3-none-any.whl')
    if args.archive:
        refresh(BUNDLE, record, args.archive, version)
    else:
        verify(BUNDLE, record)


if __name__ == '__main__':
    main()
