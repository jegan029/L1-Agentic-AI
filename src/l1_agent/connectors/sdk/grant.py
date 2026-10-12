"""The token a write capability needs (ADR 0008, ADR 0010).

A grant names one call: tenant, run, step, capability and a digest of the
exact parameters. `Connector.invoke` redeems it through a verifier before a
write runs. Grants are minted in `actions/grants.py`, and only after a policy
decision has been recorded.

A grant is signed with an Ed25519 private key that only the policy engine
holds. `GrantCheck` verifies one with the public half, so the process that
makes the write (the runner, ADR 0010) can check a grant without being able
to make one.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Set

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Signed text starts with this, so a signature made for a grant is good for nothing else.
GRANT_DOMAIN = "l1-grant-v1"


class WriteNotAuthorized(RuntimeError):
    """A write capability was invoked without an action grant from the policy engine."""


def call_digest(capability_id: str, parameters: Mapping[str, Any]) -> str:
    """SHA-256 of a capability id and its parameters, in a canonical form."""
    body = json.dumps(
        {"capability": capability_id, "parameters": dict(parameters)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(body.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Grant:
    decision_id: str
    tenant_id: str
    run_id: str
    step_id: str
    capability_id: str
    call_digest: str
    expires_at: float
    nonce: str
    signature: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional["Grant"]:
        """A grant from its wire form, or None when it is not one."""
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            return None
        texts = {k: v for k, v in data.items() if k != "expires_at"}
        expires = data["expires_at"]
        if not all(isinstance(v, str) for v in texts.values()):
            return None
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            return None
        return cls(**texts, expires_at=float(expires))


def grant_body(grant: Grant) -> bytes:
    """The exact bytes a grant's signature covers: every field but the signature."""
    return "|".join(
        [
            GRANT_DOMAIN,
            grant.decision_id,
            grant.tenant_id,
            grant.run_id,
            grant.step_id,
            grant.capability_id,
            grant.call_digest,
            repr(float(grant.expires_at)),
            grant.nonce,
        ]
    ).encode("utf-8")


class GrantCheck:
    """Accepts a grant for exactly one call, once. Needs only the public key."""

    def __init__(self, public_key: bytes, clock: Callable[[], float] = time.time) -> None:
        self._public = Ed25519PublicKey.from_public_bytes(public_key)
        self._clock = clock
        self._redeemed: Set[str] = set()

    def redeem(
        self,
        grant: Any,
        *,
        tenant_id: Any,
        run_id: str,
        step_id: str,
        capability_id: str,
        digest: str,
    ) -> None:
        """Accept a grant for exactly this call, once. Anything else raises."""
        if not isinstance(grant, Grant):
            raise WriteNotAuthorized("That is not a grant")
        try:
            self._public.verify(bytes.fromhex(grant.signature), grant_body(grant))
        except (InvalidSignature, ValueError, TypeError):
            raise WriteNotAuthorized("The grant was not issued by the policy engine") from None
        if self._clock() >= grant.expires_at:
            raise WriteNotAuthorized("The grant has expired")
        if (
            grant.tenant_id != str(tenant_id)
            or grant.run_id != str(run_id)
            or grant.step_id != step_id
            or grant.capability_id != capability_id
        ):
            raise WriteNotAuthorized("The grant is for a different step")
        if grant.call_digest != digest:
            raise WriteNotAuthorized("The grant is for different parameters")
        if grant.nonce in self._redeemed:
            raise WriteNotAuthorized("The grant has already been used")
        self._redeemed.add(grant.nonce)


class GrantVerifier(Protocol):
    async def redeem(
        self,
        grant: Any,
        *,
        tenant_id: Any,
        run_id: str,
        step_id: str,
        capability_id: str,
        digest: str,
    ) -> None:
        """Accept the grant for exactly this call, once, or raise WriteNotAuthorized."""
