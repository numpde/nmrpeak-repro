"""Verify the released acquisition closure before executing its owner."""

from hashlib import sha256
import os
import io
import json
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
        self.issuer = self.bundle.parent / 'issuer.json'
        self.issuer.write_bytes(json.dumps(self.record).encode())

    def install(self, target, archive):
        refresh(target, self.record, archive, '1.0.0', self.issuer, json.dumps(self.record).encode())

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

    def test_git_checkout_write_permissions_do_not_change_the_inventory(self):
        verify(self.bundle, self.record)
        self.member.chmod(0o664)
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
                self.install(target, archive)
        self.assertFalse(target.exists())
        previous_umask = os.umask(0o077)
        try:
            self.install(target, archive)
        finally:
            os.umask(previous_umask)
        verify(target, self.record)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual((target / 'acquisition_tool').stat().st_mode & 0o777, 0o755)
        new_parent_target = self.bundle.parent / 'new-parent' / 'acquisition'
        previous_umask = os.umask(0o077)
        try:
            self.install(new_parent_target, archive)
        finally:
            os.umask(previous_umask)
        self.assertEqual(new_parent_target.parent.stat().st_mode & 0o777, 0o755)
        (target / 'old-generated-file').write_text('old')
        previous = json.loads(self.issuer.read_bytes())
        previous['acquisition_source_files']['old-generated-file'] = {
            'size': 3, 'sha256': sha256(b'old').hexdigest(), 'mode': 0o644}
        self.issuer.write_bytes(json.dumps(previous).encode())
        original_rename = Path.rename

        def fail_publication(path, destination):
            if path.name.startswith('.acquisition-') and not path.name.startswith('.acquisition-previous-'):
                raise OSError('publish failed')
            return original_rename(path, destination)

        with patch.object(Path, 'rename', fail_publication):
            with self.assertRaises(OSError):
                self.install(target, archive)
        self.assertEqual((target / 'old-generated-file').read_text(), 'old')
        self.install(target, archive)
        verify(target, self.record)
        self.assertFalse((target / 'old-generated-file').exists())

    def test_refresh_rejects_bad_archive_before_writing(self):
        archive = self.bundle.parent / 'acquisition-owner-1.0.0.tar.gz'
        archive.write_bytes(b'bad')
        self.record['files'] = {archive.name: '0' * 64}
        target = self.bundle.parent / 'refreshed'
        with self.assertRaises(ValueError):
            self.install(target, archive)
        self.assertFalse(target.exists())

    def issued_archive(self):
        archive = self.bundle.parent / 'acquisition-owner-1.0.0.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            member = tarfile.TarInfo('acquisition_tool/owner.sh')
            member.size = 10
            member.mode = 0o644
            output.addfile(member, io.BytesIO(b'qualified\n'))
        self.record['files'] = {archive.name: sha256(archive.read_bytes()).hexdigest()}
        return archive

    def test_refresh_preserves_unrecorded_snapshot_changes(self):
        archive = self.issued_archive()
        extra = self.bundle / 'extra'
        local_directory = self.bundle / 'local-directory'
        previous_bytes = self.issuer.read_bytes()
        for mutation in ('bytes', 'execute', 'extra', 'directory'):
            with self.subTest(mutation=mutation):
                self.member.write_bytes(b'qualified\n')
                self.member.chmod(0o644)
                extra.unlink(missing_ok=True)
                if local_directory.exists():
                    local_directory.rmdir()
                if mutation == 'bytes':
                    self.member.write_bytes(b'local edit\n')
                elif mutation == 'execute':
                    self.member.chmod(0o755)
                elif mutation == 'extra':
                    extra.write_bytes(b'local member\n')
                else:
                    local_directory.mkdir()
                original = self.member.read_bytes(), self.member.stat().st_mode
                with self.assertRaises(ValueError):
                    self.install(self.bundle, archive)
                self.assertEqual(original, (self.member.read_bytes(), self.member.stat().st_mode))
                self.assertEqual(mutation == 'extra', extra.exists())
                self.assertEqual(mutation == 'directory', local_directory.is_dir())
                self.assertEqual(previous_bytes, self.issuer.read_bytes())
                self.assertFalse(list(self.bundle.parent.glob('.acquisition-*')))

    def test_issuer_publication_failure_restores_previous_source_and_record(self):
        archive = self.issued_archive()
        removed = self.bundle / 'removed'
        removed.write_bytes(b'old-only\n')
        previous = json.loads(self.issuer.read_bytes())
        previous['acquisition_source_files']['removed'] = {
            'size': 9, 'sha256': sha256(b'old-only\n').hexdigest(), 'mode': 0o644}
        previous_bytes = json.dumps(previous).encode()
        self.issuer.write_bytes(previous_bytes)
        with patch.object(bootstrap.os, 'replace', side_effect=OSError('issuer publication failed')):
            with self.assertRaisesRegex(OSError, 'issuer publication failed'):
                self.install(self.bundle, archive)
        verify(self.bundle, previous)
        self.assertEqual(previous_bytes, self.issuer.read_bytes())
        self.assertFalse(list(self.bundle.parent.glob('.acquisition-*')))

    def test_backup_cleanup_failure_reports_the_published_replacement(self):
        archive = self.issued_archive()
        with patch.object(bootstrap.shutil, 'rmtree', side_effect=OSError('backup cleanup failed')):
            with self.assertRaisesRegex(OSError, 'replacement published; cannot remove previous snapshot') as raised:
                self.install(self.bundle, archive)
        self.assertEqual('backup cleanup failed', str(raised.exception.__cause__))
        verify(self.bundle, self.record)
        self.assertEqual(json.dumps(self.record).encode(), self.issuer.read_bytes())
        backups = list(self.bundle.parent.glob('.acquisition-previous-*'))
        self.assertEqual(1, len(backups))
        verify(backups[0], self.record)

    def test_symlink_member_is_rejected(self):
        self.member.unlink()
        self.member.symlink_to('/dev/null')
        with self.assertRaises(ValueError):
            verify(self.bundle, self.record)


if __name__ == '__main__':
    unittest.main()
