"""Create the private certificate authority used by provider TLS tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def write_test_certificates(directory: Path) -> None:
    """Write one localhost certificate chain beneath ``directory``."""

    valid_from = datetime(2020, 1, 1, tzinfo=UTC)
    valid_until = datetime(2035, 1, 1, tzinfo=UTC)
    ca_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NMR test CA")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(valid_from)
        .not_valid_after(valid_until)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(2)
        .not_valid_before(valid_from)
        .not_valid_after(valid_until)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    (directory / "ca.pem").write_bytes(
        ca_certificate.public_bytes(serialization.Encoding.PEM)
    )
    (directory / "server.pem").write_bytes(
        server_certificate.public_bytes(serialization.Encoding.PEM)
    )
    (directory / "server-key.pem").write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
