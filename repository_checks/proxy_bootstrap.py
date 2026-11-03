#!/usr/bin/env python3
"""Verify or regenerate the SDK proxy bootstrap from its pinned release."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / 'dependencies/provider-core-release.json'
PIN = ROOT / 'dependencies/provider-core-source.txt'
SNAPSHOT = ROOT / 'tools/bootstrap/provider_core'
MEMBERS = ('__init__.py', 'container_guard.py', 'bound_http_proxy.py')


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


def verify_source(name, data, record):
    expected = record['source_files'][f'src/provider_core/{name}']
    if len(data) != expected['size'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
        raise ValueError(f'SDK bootstrap file does not match release record: {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, help='regenerate from this exact release wheel')
    args = parser.parse_args()
    record = json.loads(RECORD.read_text())
    wheel_name, wheel_digest = pinned_wheel(record)
    if (any(path.is_symlink() for path in (SNAPSHOT.parent.parent, SNAPSHOT.parent, SNAPSHOT))
            or (SNAPSHOT.exists() and not SNAPSHOT.is_dir())):
        raise ValueError('SDK bootstrap must be a directory without a symlink')
    if SNAPSHOT.exists() and {path.name for path in SNAPSHOT.iterdir()} - set(MEMBERS):
        raise ValueError('SDK bootstrap directory must contain exactly the release files')
    if args.wheel:
        wheel = args.wheel.read_bytes()
        if args.wheel.name != wheel_name or hashlib.sha256(wheel).hexdigest() != wheel_digest:
            raise ValueError('SDK wheel does not match the pinned release record')
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            names = archive.namelist()
            sources = {}
            for name in MEMBERS:
                member = f'provider_core/{name}'
                if names.count(member) != 1:
                    raise ValueError(f'SDK wheel must contain exactly one {member}')
                sources[name] = archive.read(member)
                verify_source(name, sources[name], record)
        SNAPSHOT.mkdir(parents=True, exist_ok=True)
        for name in sources:
            target = SNAPSHOT / name
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError(f'SDK bootstrap file must be regular and not a symlink: {name}')
        for name, source in sources.items():
            (SNAPSHOT / name).write_bytes(source)
    else:
        if {path.name for path in SNAPSHOT.iterdir()} != set(MEMBERS):
            raise ValueError('SDK bootstrap directory must contain exactly the release files')
        for name in MEMBERS:
            path = SNAPSHOT / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'SDK bootstrap file is not a regular file: {name}')
            verify_source(name, path.read_bytes(), record)


if __name__ == '__main__':
    main()
