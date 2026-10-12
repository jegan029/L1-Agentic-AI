"""A throwaway certificate authority and a server certificate signed by it.

For the chart's install test only (tools/ci/kind_install.sh): the API serves
HTTPS inside the test cluster and the runner verifies it against this CA. A
real deployment uses certificates from its own PKI.

    python tools/ci/make_tls.py <directory> <dns name> [<dns name> ...]

Writes ca.crt, tls.crt and tls.key to the directory.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def make(directory: Path, names: list) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    start, end = now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=2)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("install-test CA"))
        .issuer_name(_name("install-test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(names[0]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (directory / "tls.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "tls.key").write_bytes(_pem(key))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    make(Path(sys.argv[1]), sys.argv[2:])
