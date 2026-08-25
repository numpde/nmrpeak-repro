"""Admit one API-issued provider signing credential and matching Ed25519 key."""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass, field
import re

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_public_key,
    load_pem_private_key,
)

from .canonical_json import parse_canonical_json_bytes
from .provider_signing import validate_provider_credential_ref


PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES = 16_384
_SCHEMA_ID = "nmr.provider.private_signing_credential.v1"
_FIELDS = {
    "algorithm",
    "credential_ref",
    "principal_ref",
    "private_key_pkcs8_pem",
    "profile",
    "public_key_spki_der_b64",
    "schema_id",
}
_PROVIDER_REF = re.compile(r"provider:[A-Za-z0-9_.-]{1,119}")


@dataclass(frozen=True, slots=True)
class ProviderSigningCredential:
    """The provider identity and private signing capability from one document."""

    profile: str
    provider_ref: str
    credential_ref: str
    private_key: Ed25519PrivateKey = field(repr=False, compare=False)


def parse_provider_signing_credential(raw: bytes) -> ProviderSigningCredential:
    """Verify the closed document, key encodings, and public/private pairing."""

    if type(raw) is not bytes or len(raw) > PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES:
        raise ValueError("Provider signing credential must be bounded bytes")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise ValueError("Provider signing credential must end with one newline")
    try:
        document = parse_canonical_json_bytes(raw[:-1])
    except (TypeError, ValueError):
        raise ValueError("Provider signing credential is not canonical JSON") from None
    if type(document) is not dict or set(document) != _FIELDS:
        raise ValueError("Provider signing credential has an invalid shape")
    profile = document["profile"]
    provider_ref = document["principal_ref"]
    credential_ref = document["credential_ref"]
    if (
        document["schema_id"] != _SCHEMA_ID
        or document["algorithm"] != "ed25519"
        or type(profile) is not str
        or profile not in {"dev-local", "dev", "run"}
        or type(provider_ref) is not str
        or _PROVIDER_REF.fullmatch(provider_ref) is None
    ):
        raise ValueError("Provider signing credential identity is invalid")
    try:
        validate_provider_credential_ref(credential_ref)
    except (TypeError, ValueError):
        raise ValueError("Provider signing credential identity is invalid") from None
    encoded_public_key = document["public_key_spki_der_b64"]
    private_key_text = document["private_key_pkcs8_pem"]
    if type(encoded_public_key) is not str or type(private_key_text) is not str:
        raise ValueError("Provider signing credential key material is invalid")
    try:
        public_der = b64decode(encoded_public_key, validate=True)
        if b64encode(public_der).decode("ascii") != encoded_public_key:
            raise ValueError
        public_key = load_der_public_key(public_der)
        private_key = load_pem_private_key(
            private_key_text.encode("ascii"),
            password=None,
        )
    except (TypeError, UnicodeError, UnsupportedAlgorithm, ValueError):
        raise ValueError("Provider signing credential key material is invalid") from None
    if not isinstance(public_key, Ed25519PublicKey) or not isinstance(
        private_key,
        Ed25519PrivateKey,
    ):
        raise ValueError("Provider signing credential must contain Ed25519 keys")
    if private_key.public_key().public_bytes(
        Encoding.DER,
        PublicFormat.SubjectPublicKeyInfo,
    ) != public_der:
        raise ValueError("Provider signing credential keys do not match")
    return ProviderSigningCredential(
        profile,
        provider_ref,
        credential_ref,
        private_key,
    )
