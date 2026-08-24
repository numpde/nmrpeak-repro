"""Prove every client send freshly authenticates unchanged business bytes."""

from __future__ import annotations

from base64 import b64decode
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nmrpeak_provider.provider_api import ProviderApiClient
from nmrpeak_provider.provider_https import (
    ProviderHttpsEndpoint,
    ProviderRequestUnavailable,
    RequestDelivery,
)
from nmrpeak_provider.provider_requests import prepare_execution_attempt_start


class ProviderApiClientTests(unittest.TestCase):
    def test_each_send_refreshes_authentication_without_changing_business_bytes(self) -> None:
        client = ProviderApiClient(
            endpoint=ProviderHttpsEndpoint(
                origin="https://api.example.test",
                expected_topology="dev",
                connect_timeout_seconds=1,
                io_deadline_seconds=1,
            ),
            credential_ref="credential:provider:test",
            private_key=Ed25519PrivateKey.from_private_bytes(bytes(range(32))),
        )
        prepared = prepare_execution_attempt_start(
            job_ref="job:test",
            provider_attempt_key="nmrpeak-provider.v1:" + "1" * 64,
        )
        sent = []

        def capture_send(*, endpoint, operation, request):
            sent.append((endpoint, operation, request))
            return ProviderRequestUnavailable(RequestDelivery.NOT_SENT)

        with (
            patch("nmrpeak_provider.provider_api.time", side_effect=(100, 100)),
            patch(
                "nmrpeak_provider.provider_api.token_bytes",
                side_effect=(b"a" * 16, b"b" * 16),
            ),
            patch(
                "nmrpeak_provider.provider_api.send_provider_request",
                side_effect=capture_send,
            ),
        ):
            first = client.send(prepared)
            second = client.send(prepared)

        self.assertEqual(
            ProviderRequestUnavailable(RequestDelivery.NOT_SENT),
            first,
        )
        self.assertEqual(first, second)
        self.assertEqual(2, len(sent))
        for endpoint, operation, _request in sent:
            self.assertIs(client.endpoint, endpoint)
            self.assertIs(prepared.operation, operation)
        first_request = sent[0][2]
        second_request = sent[1][2]
        self.assertEqual(first_request.body, second_request.body)
        self.assertEqual(first_request.raw_target, second_request.raw_target)
        self.assertNotEqual(
            first_request.headers["Signature-Input"],
            second_request.headers["Signature-Input"],
        )
        self.assertNotEqual(
            first_request.headers["Signature"],
            second_request.headers["Signature"],
        )
        public_key = client.private_key.public_key()
        for request in (first_request, second_request):
            encoded_signature = request.headers["Signature"]
            signature = b64decode(encoded_signature.removeprefix("sig1=:")[:-1])
            public_key.verify(signature, request.signature_base)


if __name__ == "__main__":
    unittest.main()
