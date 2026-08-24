"""NMR API HTTP Message Signatures for exact provider request targets."""

from __future__ import annotations

from base64 import b64encode, urlsafe_b64encode
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import re
from types import MappingProxyType

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SIGNATURE_LIFETIME_SECONDS = 300
SIGNATURE_TAG = "nmr-api-v1"
_SIGNATURE_LABEL = "sig1"
_CREDENTIAL_REF = re.compile(
    r"credential:[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*"
)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_RAW_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@/-]+")
_RAW_QUERY = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@/%?-]*")


@dataclass(frozen=True, slots=True)
class SignedProviderRequest:
    """Exact target, body, and headers covered by one fresh signature."""

    method: str
    authority: str
    path: str
    query: str
    body: bytes | None
    headers: Mapping[str, str]
    signature_base: bytes = field(repr=False)

    @property
    def raw_target(self) -> str:
        """Return the exact origin-form target to put on the HTTP request line."""

        return self.path + (f"?{self.query}" if self.query else "")


def sign_provider_request(
    *,
    private_key: Ed25519PrivateKey,
    credential_ref: str,
    method: str,
    authority: str,
    path: str,
    query: str,
    body: bytes | None,
    created: int,
    nonce: bytes,
) -> SignedProviderRequest:
    """Sign one exact HTTPS request using the released NMR API profile."""

    _validate_request_inputs(
        private_key=private_key,
        credential_ref=credential_ref,
        method=method,
        authority=authority,
        path=path,
        query=query,
        body=body,
        created=created,
        nonce=nonce,
    )
    components = ["@method", "@authority", "@path", "@query"]
    values = {
        "@method": method,
        "@authority": authority,
        "@path": path,
        "@query": "?" + query,
    }
    headers = {
        "Accept": "application/json, application/problem+json",
        "Host": authority,
    }
    if body is not None:
        content_digest = "sha-256=:" + b64encode(sha256(body).digest()).decode() + ":"
        components.extend(("content-type", "content-digest"))
        values["content-type"] = "application/json"
        values["content-digest"] = content_digest
        headers["Content-Type"] = "application/json"
        headers["Content-Digest"] = content_digest

    nonce_text = urlsafe_b64encode(nonce).rstrip(b"=").decode("ascii")
    signature_parameters = _signature_parameters(
        components=components,
        created=created,
        expires=created + SIGNATURE_LIFETIME_SECONDS,
        nonce=nonce_text,
        credential_ref=credential_ref,
    )
    signature_input = f"{_SIGNATURE_LABEL}={signature_parameters}"
    signature_base = "\n".join(
        [f'"{component}": {values[component]}' for component in components]
        + [f'"@signature-params": {signature_parameters}']
    ).encode("ascii")
    signature = private_key.sign(signature_base)
    headers["Signature-Input"] = signature_input
    headers["Signature"] = f"{_SIGNATURE_LABEL}=:{b64encode(signature).decode()}:"
    return SignedProviderRequest(
        method=method,
        authority=authority,
        path=path,
        query=query,
        body=body,
        headers=MappingProxyType(headers),
        signature_base=signature_base,
    )


def validate_provider_credential_ref(value: object) -> None:
    """Admit the one credential-reference grammar used by provider signing."""

    if (
        type(value) is not str
        or len(value) > 128
        or _CREDENTIAL_REF.fullmatch(value) is None
    ):
        raise ValueError(
            "Provider request signing requires a valid credential reference"
        )


def _signature_parameters(
    *,
    components: list[str],
    created: int,
    expires: int,
    nonce: str,
    credential_ref: str,
) -> str:
    covered = " ".join(f'"{component}"' for component in components)
    return (
        f"({covered});created={created};expires={expires};nonce=\"{nonce}\";"
        f"keyid=\"{credential_ref}\";tag=\"{SIGNATURE_TAG}\""
    )


def _validate_request_inputs(
    *,
    private_key: object,
    credential_ref: object,
    method: object,
    authority: object,
    path: object,
    query: object,
    body: object,
    created: object,
    nonce: object,
) -> None:
    if not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("Provider request signing requires an Ed25519 private key")
    validate_provider_credential_ref(credential_ref)
    if type(method) is not str or method not in {"GET", "POST", "PUT"}:
        raise ValueError("Provider request signing requires an admitted HTTP method")
    if type(authority) is not str or not is_canonical_https_authority(authority):
        raise ValueError(
            "Provider request signing requires a canonical HTTPS authority"
        )
    if type(path) is not str:
        raise TypeError("Provider request path must be a string")
    try:
        path.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("Provider request path must contain only ASCII") from None
    segments = path.split("/")
    if (
        not path.startswith("/")
        or path == "/"
        or _RAW_PATH.fullmatch(path) is None
        or "" in segments[1:]
        or any(segment in {".", ".."} for segment in segments)
        or any(character in path for character in "%?#")
    ):
        raise ValueError("Provider request path is not an admitted raw path")
    if type(query) is not str:
        raise TypeError("Provider request query must be a string")
    try:
        query.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("Provider request query must contain only ASCII") from None
    if _RAW_QUERY.fullmatch(query) is None:
        raise ValueError("Provider request query is not an admitted raw query")
    if body is not None and type(body) is not bytes:
        raise TypeError("Provider request body must be exact bytes or absent")
    if type(created) is not int or created < 0:
        raise TypeError("Provider signing clock must be a non-negative integer")
    if type(nonce) is not bytes or len(nonce) < 16:
        raise TypeError("Provider signing nonce must contain at least 16 bytes")


def is_canonical_https_authority(value: str) -> bool:
    """Return whether ``value`` is the provider profile's HTTPS authority."""

    host, separator, port = value.rpartition(":")
    if not separator:
        host = value
        port = ""
    elif (
        not port.isascii()
        or not port.isdigit()
        or (len(port) > 1 and port.startswith("0"))
    ):
        return False
    if not host or len(host) > 253:
        return False
    labels = host.split(".")
    if any(
        len(label) > 63 or _DNS_LABEL.fullmatch(label) is None
        for label in labels
    ):
        return False
    if port:
        port_number = int(port)
        if port_number == 443 or not 1 <= port_number <= 65_535:
            return False
    return True
