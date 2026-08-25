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
from nmrpeak_provider.provider_credential import parse_provider_signing_credential
from nmrpeak_provider.provider_identity_lock import (
    ProviderIdentityLock,
    ProviderIdentityLockBusy,
)
from nmrpeak_provider.provider_startup import run_locked_provider
from tests.unit.test_generation_runtime import generation_runtime


PROVIDER_REF = "provider:nmrpeak"


class ProviderCredentialTests(unittest.TestCase):
    def test_valid_document_returns_the_matching_private_capability(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        credential = parse_provider_signing_credential(credential_bytes(private_key))
        message = b"provider startup proof"

        credential.private_key.public_key().verify(
            credential.private_key.sign(message),
            message,
        )
        self.assertEqual(credential.profile, "run")
        self.assertEqual(credential.provider_ref, PROVIDER_REF)
        self.assertEqual(credential.credential_ref, "credential:run-nmrpeak-ed25519")

    def test_shape_identity_and_canonical_spelling_are_closed(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        valid = credential_document(private_key)
        invalid = (
            {**valid, "extra": True},
            {**valid, "profile": "staging"},
            {**valid, "principal_ref": "principal:user:test"},
            {**valid, "credential_ref": "credential:contains space"},
        )
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(ValueError):
                parse_provider_signing_credential(
                    canonical_json_bytes(document) + b"\n"
                )
        with self.assertRaisesRegex(ValueError, "canonical JSON"):
            parse_provider_signing_credential(b'{"schema_id": "drift"}\n')

    def test_mismatched_public_and_private_keys_are_rejected(self) -> None:
        document = credential_document(Ed25519PrivateKey.generate())
        other = Ed25519PrivateKey.generate().public_key().public_bytes(
            Encoding.DER,
            PublicFormat.SubjectPublicKeyInfo,
        )
        document["public_key_spki_der_b64"] = b64encode(other).decode("ascii")

        with self.assertRaisesRegex(ValueError, "keys do not match"):
            parse_provider_signing_credential(canonical_json_bytes(document) + b"\n")


class ProviderIdentityLockTests(unittest.TestCase):
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
                with self.subTest(path=path), self.assertRaisesRegex(
                    ValueError,
                    "unavailable",
                ):
                    ProviderIdentityLock.acquire(path, PROVIDER_REF)


class LockedProviderStartupTests(unittest.TestCase):
    def test_invalid_or_foreign_credential_never_reaches_the_lock(self) -> None:
        foreign = credential_document(Ed25519PrivateKey.generate())
        foreign["principal_ref"] = "provider:other"
        for raw in (b"invalid", canonical_json_bytes(foreign) + b"\n"):
            with (
                self.subTest(raw=raw),
                patch(
                    "nmrpeak_provider.provider_startup.ProviderIdentityLock.acquire"
                ) as acquire,
                self.assertRaises(ValueError),
            ):
                call_locked_provider(raw)
            acquire.assert_not_called()

    def test_lock_surrounds_client_construction_and_provider_failure(self) -> None:
        events: list[str] = []

        class HeldLock:
            def __enter__(self) -> HeldLock:
                events.append("lock_entered")
                return self

            def __exit__(self, *exc_info: object) -> None:
                events.append("lock_closed")

        def acquire(_path: Path, _provider_ref: str) -> HeldLock:
            events.append("lock_acquired")
            return HeldLock()

        def client(*_args: object) -> object:
            events.append("client_created")
            return object()

        def process(**_kwargs: object) -> None:
            events.append("process_entered")
            raise RuntimeError("provider failed")

        with (
            patch(
                "nmrpeak_provider.provider_startup.ProviderIdentityLock.acquire",
                side_effect=acquire,
            ),
            patch(
                "nmrpeak_provider.provider_startup.ProviderApiClient",
                side_effect=client,
            ),
            patch(
                "nmrpeak_provider.provider_startup.run_provider_process",
                side_effect=process,
            ),
            self.assertRaisesRegex(RuntimeError, "provider failed"),
        ):
            call_locked_provider(credential_bytes(Ed25519PrivateKey.generate()))

        self.assertEqual(
            events,
            [
                "lock_acquired",
                "lock_entered",
                "client_created",
                "process_entered",
                "lock_closed",
            ],
        )

def credential_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return canonical_json_bytes(credential_document(private_key)) + b"\n"


def call_locked_provider(raw: bytes) -> None:
    run_locked_provider(
        credential_bytes=raw,
        endpoint=object(),
        runtime=generation_runtime(),
        identity_lock_path=Path("/run/nmrpeak/provider.lock"),
        journal=object(),
        hf_session=object(),
        chf_session=object(),
        hello=object(),
        policy=object(),
        stop=object(),
    )


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
