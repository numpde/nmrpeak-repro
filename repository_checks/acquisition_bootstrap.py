"""Verify or refresh the SDK bootstrap during quiescent source maintenance."""

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
    expected_directories = set()
    for name in inventory:
        path = PurePosixPath(name)
        if not isinstance(name, str) or path.is_absolute() or '..' in path.parts or path.as_posix() != name:
            raise ValueError('Acquisition bootstrap release inventory contains an invalid path.')
        expected.add(name)
        expected_directories.update(parent.as_posix() for parent in path.parents if parent != PurePosixPath('.'))
    actual = set()
    for current, directories, files in os.walk(bundle, followlinks=False):
        parent = Path(current)
        for name in directories:
            if (parent / name).is_symlink():
                raise ValueError('Acquisition bootstrap contains a symlink directory.')
            if (parent / name).relative_to(bundle).as_posix() not in expected_directories:
                raise ValueError('Acquisition bootstrap directories differ from the release inventory.')
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


def refresh(bundle: Path, record: dict, archive_path: Path, version: str,
            record_output: Path, record_bytes: bytes) -> None:
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
    parent_created = not bundle.parent.exists()
    bundle.parent.mkdir(parents=True, exist_ok=True)
    if parent_created:
        bundle.parent.chmod(0o755)
    if not bundle.parent.is_dir():
        raise ValueError('Acquisition bootstrap parent must be a directory.')
    if bundle.is_symlink() or (bundle.exists() and not bundle.is_dir()):
        raise ValueError('Acquisition bootstrap destination must be a non-symlink directory.')
    if bundle.exists():
        verify(bundle, json.loads(record_output.read_bytes()))
    stage = Path(tempfile.mkdtemp(prefix='.acquisition-', dir=bundle.parent))
    backup = None
    staged_record = None
    try:
        with tempfile.NamedTemporaryFile(dir=record_output.parent, prefix='.acquisition-record-', delete=False) as output:
            staged_record = Path(output.name)
            output.write(record_bytes)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        for name, content in contents.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(inventory[name]['mode'])
        for directory, _children, _files in os.walk(stage):
            Path(directory).chmod(0o755)
        verify(stage, record)
        if bundle.exists():
            backup = Path(tempfile.mkdtemp(prefix='.acquisition-previous-', dir=bundle.parent))
            backup.rmdir()
            bundle.rename(backup)
        try:
            stage.rename(bundle)
            os.replace(staged_record, record_output)
        except BaseException:
            if not stage.exists() and bundle.exists():
                bundle.rename(stage)
            if backup is not None:
                backup.rename(bundle)
                backup = None
            raise
        if backup is not None:
            try:
                shutil.rmtree(backup)
            except OSError as error:
                raise OSError(f'Acquisition bootstrap replacement published; cannot remove previous snapshot at {backup}.') from error
            backup = None
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if staged_record is not None:
            staged_record.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='install this exact issued acquisition source bundle')
    parser.add_argument('--issuer', type=Path, help='incoming release record for an explicit refresh')
    args = parser.parse_args()
    if args.archive is None:
        if args.issuer is not None:
            raise ValueError('An incoming issuer requires an acquisition archive.')
        record = json.loads(RECORD.read_bytes())
        pinned_wheel(record)
        verify(BUNDLE, record)
        return
    if args.issuer is None:
        raise ValueError('Refresh requires the incoming release record.')
    record_bytes = args.issuer.read_bytes()
    record = json.loads(record_bytes)
    wheel_name, _ = pinned_wheel(record)
    version = wheel_name.removeprefix('provider_core-').removesuffix('-py3-none-any.whl')
    refresh(BUNDLE, record, args.archive, version, RECORD, record_bytes)


if __name__ == '__main__':
    main()
