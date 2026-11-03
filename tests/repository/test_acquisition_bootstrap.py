"""Verify the released acquisition closure before executing its owner."""

from hashlib import sha256
import io
import tarfile
from unittest.mock import patch
from pathlib import Path
import tempfile
import unittest

from repository_checks import acquisition_bootstrap as bootstrap
from repository_checks.acquisition_bootstrap import refresh, verify


class AcquisitionBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.bundle = Path(temporary.name) / 'bundle'
        self.member = self.bundle / 'acquisition_tool/owner.sh'
        self.member.parent.mkdir(parents=True)
        self.member.write_bytes(b'qualified\n')
        self.member.chmod(0o644)
        self.record = {'acquisition_source_files': {'acquisition_tool/owner.sh': {
            'sha256': sha256(b'qualified\n').hexdigest(), 'size': 10, 'mode': 0o644,
        }}}

    def test_release_record_must_match_runtime_wheel_pin(self):
        pin = self.bundle.parent / 'source.txt'
        digest = 'a' * 64
        pin.write_text('provider-core @ https://github.com/numpde/nmr-provider-tools/releases/download/v1.0.0/'
                       'provider_core-1.0.0-py3-none-any.whl \\\n    --hash=sha256:' + digest + '\n')
        record = {'files': {'provider_core-1.0.0-py3-none-any.whl': digest}}
        with patch.object(bootstrap, 'PIN', pin):
            bootstrap.pinned_wheel(record)
            record['files']['provider_core-1.0.0-py3-none-any.whl'] = 'b' * 64
            with self.assertRaises(ValueError):
                bootstrap.pinned_wheel(record)

    def test_exact_inventory_is_accepted(self):
        verify(self.bundle, self.record)

    def test_missing_extra_or_changed_member_is_rejected(self):
        for change in ('missing', 'extra', 'changed', 'mode'):
            with self.subTest(change=change):
                self.member.write_bytes(b'qualified\n')
                self.member.chmod(0o644)
                extra = self.bundle / 'extra'
                extra.unlink(missing_ok=True)
                if change == 'missing': self.member.unlink()
                elif change == 'extra': extra.touch()
                elif change == 'changed': self.member.write_bytes(b'tampered\n')
                else: self.member.chmod(0o755)
                with self.assertRaises(ValueError):
                    verify(self.bundle, self.record)

    def test_refresh_retries_after_staging_write_fails(self):
        archive = self.bundle.parent / 'acquisition-owner-1.0.0.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            member = tarfile.TarInfo('acquisition_tool/owner.sh')
            member.size = 10
            member.mode = 0o644
            output.addfile(member, io.BytesIO(b'qualified\n'))
        self.record['files'] = {archive.name: sha256(archive.read_bytes()).hexdigest()}
        target = self.bundle.parent / 'refreshed'
        with patch.object(Path, 'write_bytes', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                refresh(target, self.record, archive, '1.0.0')
        self.assertFalse(target.exists())
        refresh(target, self.record, archive, '1.0.0')
        verify(target, self.record)
        (target / 'old-generated-file').write_text('old')
        original_rename = Path.rename

        def fail_publication(path, destination):
            if path.name.startswith('.acquisition-') and not path.name.startswith('.acquisition-previous-'):
                raise OSError('publish failed')
            return original_rename(path, destination)

        with patch.object(Path, 'rename', fail_publication):
            with self.assertRaises(OSError):
                refresh(target, self.record, archive, '1.0.0')
        self.assertEqual((target / 'old-generated-file').read_text(), 'old')
        refresh(target, self.record, archive, '1.0.0')
        verify(target, self.record)
        self.assertFalse((target / 'old-generated-file').exists())

    def test_refresh_rejects_bad_archive_before_writing(self):
        archive = self.bundle.parent / 'acquisition-owner-1.0.0.tar.gz'
        archive.write_bytes(b'bad')
        self.record['files'] = {archive.name: '0' * 64}
        target = self.bundle.parent / 'refreshed'
        with self.assertRaises(ValueError):
            refresh(target, self.record, archive, '1.0.0')
        self.assertFalse(target.exists())

    def test_symlink_member_is_rejected(self):
        self.member.unlink()
        self.member.symlink_to('/dev/null')
        with self.assertRaises(ValueError):
            verify(self.bundle, self.record)


if __name__ == '__main__':
    unittest.main()
