"""Minting and checking grants (ADR 0008, ADR 0010).

A grant is signed with an Ed25519 private key that never leaves this object.
By default the key is random, so a grant can only come from the authority
that will be asked to check it. A deployment with a separate runner gives the
authority a key of its own (`ACTIONS_GRANT_KEY`), and the runner checks grants
with the public half: it can verify a grant and cannot make one.

`mint` is called from one place, `ActionService._authorise`, after the
database has recorded that the action may execute; a test searches the source
tree to keep it that way.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.l1_agent.connectors.sdk.grant import Grant, GrantCheck, grant_body

GRANT_SECONDS = 60.0


class GrantAuthority:
    def __init__(
        self,
        ttl_seconds: float = GRANT_SECONDS,
        clock: Callable[[], float] = time.time,
        private_key: Optional[bytes] = None,
    ) -> None:
        if private_key is not None and len(private_key) != 32:
            raise ValueError("A grant signing key is 32 bytes")
        self._key = private_key or secrets.token_bytes(32)
        self._signer = Ed25519PrivateKey.from_private_bytes(self._key)
        self._ttl = ttl_seconds
        self._clock = clock
        self._check = GrantCheck(self.public_key, clock)

    @property
    def public_key(self) -> bytes:
        """What a runner needs to check this authority's grants. Not a secret."""
        return self._signer.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def mint(
        self,
        *,
        decision_id: str,
        tenant_id: str,
        run_id: str,
        step_id: str,
        capability_id: str,
        call_digest: str,
    ) -> Grant:
        if not decision_id:
            raise ValueError("A grant is minted for a recorded decision")
        unsigned = Grant(
            decision_id=decision_id,
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=step_id,
            capability_id=capability_id,
            call_digest=call_digest,
            expires_at=self._clock() + self._ttl,
            nonce=secrets.token_hex(16),
            signature="",
        )
        signature = self._signer.sign(grant_body(unsigned)).hex()
        return Grant(**{**unsigned.__dict__, "signature": signature})

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
        self._check.redeem(
            grant,
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=step_id,
            capability_id=capability_id,
            digest=digest,
        )
