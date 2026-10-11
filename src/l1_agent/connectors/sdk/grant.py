"""The token a write capability needs (ADR 0008).

A grant names one call: tenant, run, step, capability and a digest of the
exact parameters. `Connector.invoke` redeems it through a verifier before a
write runs. Grants are minted in `actions/grants.py`, and only after a policy
decision has been recorded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


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
