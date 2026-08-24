"""Prove provider request signatures match the released HTTP profile."""

from __future__ import annotations

from base64 import b64decode
import json
from pathlib import Path
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import load_der_public_key

from nmrpeak_provider.provider_signing import sign_provider_request


CONTRACT_ROOT = Path(__file__).parents[2] / "contracts/upstream/nmr_api_v1"
PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def signing_vectors() -> list[dict[str, object]]:
    openapi = json.loads(
        (CONTRACT_ROOT / "openapi/openapi.v1.json").read_text(encoding="utf-8")
    )
    return openapi["x-nmr-signature-conformance-vectors"]


class ProviderSigningTests(unittest.TestCase):
    def test_released_signature_bases_and_inputs_are_exact(self) -> None:
        for vector in signing_vectors():
            with self.subTest(name=vector["name"]):
                request = sign_provider_request(
                    private_key=PRIVATE_KEY,
                    credential_ref=vector["keyid"],
                    method=vector["method"],
                    authority=vector["authority"],
                    path=vector["raw_path"],
                    query=vector["raw_query"],
                    body=(
                        vector["body_utf8"].encode("utf-8")
                        if vector["body_utf8"] is not None
                        else None
                    ),
                    created=vector["created"],
                    nonce=_decode_nonce(vector["nonce"]),
                )
                self.assertEqual(
                    vector["signature_input"],
                    request.headers["Signature-Input"],
                )
                self.assertEqual(
                    vector["signature_base"].encode("ascii"),
                    request.signature_base,
                )
                self.assertEqual(
                    vector["content_digest"],
                    request.headers.get("Content-Digest"),
                )

    def test_released_signatures_verify_against_released_public_keys(self) -> None:
        for vector in signing_vectors():
            with self.subTest(name=vector["name"]):
                public_key = load_der_public_key(
                    b64decode(vector["public_key_spki_der_b64"])
                )
                self.assertIsInstance(public_key, Ed25519PublicKey)
                signature = b64decode(
                    vector["signature"].removeprefix("sig1=:").removesuffix(":")
                )
                public_key.verify(
                    signature,
                    vector["signature_base"].encode("ascii"),
                )

    def test_bodyless_and_body_requests_cover_different_components(self) -> None:
        bodyless, with_body = signing_vectors()
        for vector, expected_digest in (
            (bodyless, False),
            (with_body, True),
        ):
            request = sign_provider_request(
                private_key=PRIVATE_KEY,
                credential_ref=vector["keyid"],
                method=vector["method"],
                authority=vector["authority"],
                path=vector["raw_path"],
                query=vector["raw_query"],
                body=(
                    vector["body_utf8"].encode()
                    if vector["body_utf8"] is not None
                    else None
                ),
                created=vector["created"],
                nonce=_decode_nonce(vector["nonce"]),
            )
            self.assertEqual(expected_digest, "Content-Digest" in request.headers)

    def test_emitted_signature_header_verifies_for_a_longer_nonce(self) -> None:
        request = sign_provider_request(
            private_key=PRIVATE_KEY,
            credential_ref="credential:provider:test",
            method="GET",
            authority="api.example.test",
            path="/provider/v1/jobs",
            query="limit=1",
            body=None,
            created=1_700_000_000,
            nonce=bytes(range(32)),
        )
        signature = b64decode(
            request.headers["Signature"].removeprefix("sig1=:").removesuffix(":")
        )
        PRIVATE_KEY.public_key().verify(signature, request.signature_base)

    def test_signature_inputs_are_strict_and_safe(self) -> None:
        valid = {
            "private_key": PRIVATE_KEY,
            "credential_ref": "credential:provider:test",
            "method": "GET",
            "authority": "api.example.test",
            "path": "/provider/v1/jobs",
            "query": "limit=1",
            "body": None,
            "created": 1_700_000_000,
            "nonce": bytes(range(16)),
        }
        cases = (
            ("credential_ref", "credential:contains space"),
            ("method", "get"),
            ("authority", "API.example.test"),
            ("authority", "api.example.test:443"),
            ("authority", "api.example.test:00444"),
            ("authority", "api..example.test"),
            ("authority", "api.example.test:99999"),
            ("path", "/provider//jobs"),
            ("path", "/provider/has space"),
            ("path", "/provider/%2e/jobs"),
            ("query", "limit=1#fragment"),
            ("created", True),
            ("nonce", b"short"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                with self.assertRaises((TypeError, ValueError)):
                    sign_provider_request(**{**valid, field: value})

    def test_raw_target_does_not_invent_a_query_delimiter(self) -> None:
        request = sign_provider_request(
            private_key=PRIVATE_KEY,
            credential_ref="credential:provider:test",
            method="POST",
            authority="api.example.test",
            path="/provider/v1/hello",
            query="",
            body=b"{}",
            created=1_700_000_000,
            nonce=bytes(range(16)),
        )
        self.assertEqual("/provider/v1/hello", request.raw_target)
        self.assertIn('"@query": ?', request.signature_base.decode())
        with self.assertRaises(TypeError):
            request.headers["Host"] = "rewritten.example"


def _decode_nonce(value: str) -> bytes:
    return b64decode(value + "==", altchars=b"-_")


if __name__ == "__main__":
    unittest.main()
