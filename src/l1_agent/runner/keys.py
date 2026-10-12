"""Ed25519 keys for the runner protocol (ADR 0010).

Three key pairs are in use. Each private key stays where it was made.

    dispatch key   the control plane signs every call it hands to a runner
    grant key      the policy engine signs every grant for a write (actions/grants.py)
    runner key     a runner signs every request it makes to the control plane

A private key is kept as a PEM file (PKCS#8) and read through a credential
reference. A public key is 32 bytes, written as base64 so it fits in a
setting, a Helm value or an enrolment form.
"""

from __future__ import annotations

import base64
import binascii
from typing import Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from src.l1_agent.config.settings import ConfigError


def generate() -> Tuple[str, str]:
    """A new key pair: (private key as PEM, public key as base64)."""
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return pem, public_text(_public_bytes(private))


def _public_bytes(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def public_of(seed: bytes) -> bytes:
    """The public half of a private key given as its 32-byte seed."""
    return _public_bytes(Ed25519PrivateKey.from_private_bytes(seed))


def public_text(public_key: bytes) -> str:
    return base64.b64encode(public_key).decode("ascii")


def parse_public(text: str) -> bytes:
    """The 32 bytes of a public key given as base64. Raises ConfigError otherwise."""
    try:
        raw = base64.b64decode((text or "").strip(), validate=True)
        Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError):
        raise ConfigError("A public key is 32 bytes of Ed25519, written as base64") from None
    return raw


def private_seed(text: str) -> bytes:
    """The 32-byte seed of a private key given as PEM (PKCS#8). Raises ConfigError otherwise."""
    try:
        key = serialization.load_pem_private_key((text or "").strip().encode("ascii"), None)
    except (ValueError, TypeError, UnicodeEncodeError):
        raise ConfigError("A private key is an Ed25519 key in PEM (PKCS#8) form") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ConfigError("A private key is an Ed25519 key in PEM (PKCS#8) form")
    return key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


class Signer:
    """Signs with one private key, for one purpose.

    `domain` is put in front of everything signed, so a signature made for one
    purpose (a call, a request) cannot be passed off as another (a grant).
    """

    def __init__(self, seed: bytes, domain: str) -> None:
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        self._domain = domain.encode("ascii") + b"\n"

    @property
    def public_key(self) -> bytes:
        return _public_bytes(self._key)

    def sign(self, message: bytes) -> str:
        return self._key.sign(self._domain + message).hex()


def verify(public_key: bytes, domain: str, message: bytes, signature: str) -> bool:
    """True when `signature` is this key's signature of `message` for `domain`."""
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            bytes.fromhex(signature), domain.encode("ascii") + b"\n" + message
        )
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True
