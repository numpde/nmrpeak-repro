"""Own fresh authentication for every outbound NMR API operation send."""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import token_bytes
from time import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .provider_https import (
    ProviderHttpsEndpoint,
    ProviderHttpsOutcome,
    send_provider_request,
)
from .provider_requests import (
    _PreparedProviderRequest,
    sign_prepared_provider_request,
)
from .provider_signing import validate_provider_credential_ref


@dataclass(frozen=True, slots=True)
class ProviderApiClient:
    """The authenticated transport authority for one provider credential."""

    endpoint: ProviderHttpsEndpoint
    credential_ref: str
    private_key: Ed25519PrivateKey = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.endpoint) is not ProviderHttpsEndpoint:
            raise TypeError("Provider API client requires an admitted HTTPS endpoint")
        if not isinstance(self.private_key, Ed25519PrivateKey):
            raise TypeError("Provider API client requires an Ed25519 private key")
        validate_provider_credential_ref(self.credential_ref)

    def send(self, prepared: _PreparedProviderRequest) -> ProviderHttpsOutcome:
        """Sign and send one operation with a fresh timestamp and nonce."""

        signed = sign_prepared_provider_request(
            prepared,
            private_key=self.private_key,
            credential_ref=self.credential_ref,
            authority=self.endpoint.authority,
            created=int(time()),
            nonce=token_bytes(16),
        )
        return send_provider_request(
            endpoint=self.endpoint,
            operation=prepared.operation,
            request=signed,
        )
