"""Minting and checking grants (ADR 0008).

The signing key is random and never leaves this object, so a grant can only
come from the authority that will be asked to check it. `mint` is called from
one place, `ActionService._authorise`, after the database has recorded that
the action may execute; a test searches the source tree to keep it that way.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any, Callable, Set

from src.l1_agent.connectors.sdk.base import WriteNotAuthorized
from src.l1_agent.connectors.sdk.grant import Grant

GRANT_SECONDS = 60.0


class GrantAuthority:
    def __init__(
        self, ttl_seconds: float = GRANT_SECONDS, clock: Callable[[], float] = time.time
    ) -> None:
        self._key = secrets.token_bytes(32)
        self._ttl = ttl_seconds
        self._clock = clock
        self._redeemed: Set[str] = set()

    def _sign(self, grant: Grant) -> str:
        body = "|".join(
            [
                grant.decision_id,
                grant.tenant_id,
                grant.run_id,
                grant.step_id,
                grant.capability_id,
                grant.call_digest,
                repr(grant.expires_at),
                grant.nonce,
            ]
        )
        return hmac.new(self._key, body.encode("utf-8"), hashlib.sha256).hexdigest()

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
        return Grant(**{**unsigned.__dict__, "signature": self._sign(unsigned)})

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
        if not hmac.compare_digest(grant.signature, self._sign(grant)):
            raise WriteNotAuthorized("The grant was not issued by the policy engine")
        if self._clock() >= grant.expires_at:
            raise WriteNotAuthorized("The grant has expired")
        if (
            grant.tenant_id != str(tenant_id)
            or grant.run_id != str(run_id)
            or grant.step_id != step_id
            or grant.capability_id != capability_id
        ):
            raise WriteNotAuthorized("The grant is for a different step")
        if not hmac.compare_digest(grant.call_digest, digest):
            raise WriteNotAuthorized("The grant is for different parameters")
        if grant.nonce in self._redeemed:
            raise WriteNotAuthorized("The grant has already been used")
        self._redeemed.add(grant.nonce)
