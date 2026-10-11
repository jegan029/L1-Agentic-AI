"""Break-glass administrator: one local account for when the identity provider is down.

Disabled unless BREAK_GLASS_PASSWORD_HASH is set. Every use, successful or
not, is written to the audit log. The hash is scrypt; create one with
`l1-agent breakglass hash`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

N, R, P, LENGTH = 2**15, 8, 1, 32
MIN_PASSWORD_LENGTH = 16
SUBJECT = "break-glass"
ISSUER = "local"


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=LENGTH, maxmem=128 * 1024 * 1024
    )


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"The break-glass password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    salt = secrets.token_bytes(16)
    derived = _derive(password, salt, N, R, P)
    encode = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${N}${R}${P}${encode(salt)}${encode(derived)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. False for an empty or malformed stored hash."""
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = _derive(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(derived, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False
