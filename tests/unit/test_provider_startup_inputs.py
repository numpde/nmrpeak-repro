"""Prove credential and singleton-lock admission before provider activation."""

from __future__ import annotations

from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import os
import stat
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.provider_identity_lock import (
    ProviderIdentityLock,
    ProviderIdentityLockBusy,
)


PROVIDER_REF = "provider:nmrpeak"


class ProviderIdentityLockTests(unittest.TestCase):
    def test_volume_identity_is_admitted_and_excludes_expected_identity_owner(self):
        with lock_file() as path, root_owned_lock_status(path):
            owner = ProviderIdentityLock.acquire_volume(path)
            try:
                self.assertEqual(owner.provider_ref, PROVIDER_REF)
                with self.assertRaises(ProviderIdentityLockBusy):
                    ProviderIdentityLock.acquire(path, PROVIDER_REF)
            finally:
                owner.close()

    def test_volume_identity_rejects_bad_framing(self):
        for content in (b"provider:nmrpeak", b"provider:nmrpeak\n\n", b"\xff\n"):
            with self.subTest(content=content), lock_file(content=content) as path, root_owned_lock_status(path):
                with self.assertRaisesRegex(ValueError, "invalid identity"):
                    ProviderIdentityLock.acquire_volume(path)

    def test_failed_close_remains_visible_without_retrying_a_reused_descriptor(self):
        failure = OSError("Descriptor release acknowledgement failed")
        with lock_file() as path, root_owned_lock_status(path):
            owner = ProviderIdentityLock.acquire(path, PROVIDER_REF)
            real_close = os.close
            replacement = []

            def close_then_reuse(descriptor):
                real_close(descriptor)
                reopened = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                if reopened != descriptor:
                    os.dup2(reopened, descriptor)
                    real_close(reopened)
                replacement.append(descriptor)
                raise failure

            try:
                with patch("nmrpeak_provider.provider_identity_lock.os.close", side_effect=close_then_reuse) as close:
                    for _ in range(2):
                        with self.assertRaises(OSError) as caught:
                            owner.close()
                        self.assertIs(caught.exception, failure)
                    close.assert_called_once()
                self.assertTrue(stat.S_ISREG(os.fstat(replacement[0]).st_mode))
            finally:
                for descriptor in replacement:
                    real_close(descriptor)

    def test_context_retains_the_primary_and_independent_close_failure(self):
        primary = RuntimeError("Provider failed before cleanup")
        cleanup = OSError("Descriptor close failed")
        with lock_file() as path, root_owned_lock_status(path):
            owner = ProviderIdentityLock.acquire(path, PROVIDER_REF)
            real_close = os.close

            def failed_close(descriptor):
                real_close(descriptor)
                raise cleanup

            with patch("nmrpeak_provider.provider_identity_lock.os.close", side_effect=failed_close):
                with self.assertRaises(BaseExceptionGroup) as caught:
                    with owner:
                        raise primary
            self.assertEqual(caught.exception.exceptions, (primary, cleanup))

    def test_admission_refusal_retains_an_independent_descriptor_close_failure(self):
        cleanup = OSError("Refused descriptor close failed")
        with lock_file(content=b"provider:other\n") as path, root_owned_lock_status(path):
            real_close = os.close

            def failed_close(descriptor):
                real_close(descriptor)
                raise cleanup

            with patch("nmrpeak_provider.provider_identity_lock.os.close", side_effect=failed_close):
                with self.assertRaises(BaseExceptionGroup) as caught:
                    ProviderIdentityLock.acquire(path, PROVIDER_REF)
            self.assertIsInstance(caught.exception.exceptions[0], ValueError)
            self.assertIn("another provider", str(caught.exception.exceptions[0]))
            self.assertIs(caught.exception.exceptions[1], cleanup)

    def test_lock_is_held_until_close_and_excludes_a_second_owner(self) -> None:
        with lock_file() as path, root_owned_lock_status(path):
            first = ProviderIdentityLock.acquire(path, PROVIDER_REF)
            try:
                with self.assertRaises(ProviderIdentityLockBusy):
                    ProviderIdentityLock.acquire(path, PROVIDER_REF)
            finally:
                first.close()
            second = ProviderIdentityLock.acquire(path, PROVIDER_REF)
            second.close()

    def test_lock_rejects_wrong_identity_or_file_posture(self) -> None:
        with lock_file(content=b"provider:other\n") as path, root_owned_lock_status(path):
            with self.assertRaisesRegex(ValueError, "another provider"):
                ProviderIdentityLock.acquire(path, PROVIDER_REF)

        with lock_file() as path:
            actual = path.stat()
            for owner, mode in ((1000, 0o444), (0, 0o644)):
                status = SimpleNamespace(
                    st_mode=stat.S_IFREG | mode,
                    st_uid=owner,
                    st_size=actual.st_size,
                )
                with (
                    self.subTest(owner=owner, mode=mode),
                    patch(
                        "nmrpeak_provider.provider_identity_lock.os.fstat",
                        return_value=status,
                    ),
                    self.assertRaisesRegex(ValueError, "root-owned mode-0444"),
                ):
                    ProviderIdentityLock.acquire(path, PROVIDER_REF)

    def test_missing_and_symlinked_lock_files_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_bytes(PROVIDER_REF.encode() + b"\n")
            link = root / "lock"
            os.symlink(target, link)
            for path in (root / "missing", link):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, "unavailable") as raised:
                        ProviderIdentityLock.acquire(path, PROVIDER_REF)
                    self.assertIsInstance(raised.exception.__cause__, OSError)


def credential_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return canonical_json_bytes(credential_document(private_key)) + b"\n"


def credential_document(private_key: Ed25519PrivateKey) -> dict[str, object]:
    public_der = private_key.public_key().public_bytes(
        Encoding.DER,
        PublicFormat.SubjectPublicKeyInfo,
    )
    private_pem = private_key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    ).decode("ascii")
    return {
        "schema_id": "nmr.provider.private_signing_credential.v1",
        "profile": "run",
        "principal_ref": PROVIDER_REF,
        "credential_ref": "credential:run-nmrpeak-ed25519",
        "algorithm": "ed25519",
        "public_key_spki_der_b64": b64encode(public_der).decode("ascii"),
        "private_key_pkcs8_pem": private_pem,
    }


class lock_file:
    def __init__(self, content: bytes | None = None) -> None:
        self.content = content or PROVIDER_REF.encode() + b"\n"
        self.temporary = TemporaryDirectory()

    def __enter__(self) -> Path:
        path = Path(self.temporary.name) / "provider.lock"
        path.write_bytes(self.content)
        path.chmod(0o444)
        return path

    def __exit__(self, *exc_info: object) -> None:
        self.temporary.cleanup()


class root_owned_lock_status:
    def __init__(self, path: Path) -> None:
        actual = path.stat()
        self.status = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o444,
            st_uid=0,
            st_size=actual.st_size,
        )
        self.patcher = patch(
            "nmrpeak_provider.provider_identity_lock.os.fstat",
            return_value=self.status,
        )

    def __enter__(self) -> None:
        self.patcher.start()

    def __exit__(self, *exc_info: object) -> None:
        self.patcher.stop()


if __name__ == "__main__":
    unittest.main()
