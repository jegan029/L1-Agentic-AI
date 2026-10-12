"""What travels between the control plane and a runner (ADR 0010).

The runner makes every connection, outbound, to two routes:

    POST /runner/v1/claim                  ask for calls; held open while there are none
    POST /runner/v1/calls/{id}/result      return one call's result

Each request is signed with the runner's key. Each call the runner is handed
is an envelope signed with the control plane's dispatch key. A write also
carries a grant signed with the policy engine's grant key. No credential is
ever in an envelope: it holds references, which the runner resolves itself.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.l1_agent.adapters.base import AdapterResult

CALL_DOMAIN = "l1-runner-call-v1"
REQUEST_DOMAIN = "l1-runner-request-v1"

ID_HEADER = "X-Runner-Id"
TIMESTAMP_HEADER = "X-Runner-Timestamp"
SIGNATURE_HEADER = "X-Runner-Signature"
MAX_SKEW_SECONDS = 60

CLAIM_PATH = "/runner/v1/claim"
MAX_WAIT_SECONDS = 20.0
MAX_CLAIM = 10

KIND_INVOKE = "invoke"
KIND_DRY_RUN = "dry_run"
KIND_TEST = "test"
KINDS = (KIND_INVOKE, KIND_DRY_RUN, KIND_TEST)

# How a call ended on the runner. Only `ok` carries a connector's own result.
OK = "ok"
REFUSED = "refused"  # the call breaks a schema, an allow list or the runner's ceiling
NOT_AUTHORIZED = "not_authorized"  # a write without a grant the runner accepts
UNKNOWN = "unknown"  # no such capability, or the connector is not available on the runner
ERROR = "error"  # the runner could not make the call
OUTCOMES = (OK, REFUSED, NOT_AUTHORIZED, UNKNOWN, ERROR)

# A result must fit in one request (api/security.py: MAX_BODY_BYTES).
MAX_RAW_OUTPUT = 64 * 1024
MAX_EVIDENCE = 16 * 1024
MAX_DATA = 96 * 1024
MAX_ERROR = 2000


def canonical(value: Any) -> str:
    """One text for one value: what is signed, and what is compared."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def result_path(call_id: Any) -> str:
    return f"/runner/v1/calls/{call_id}/result"


# ── Who is calling ────────────────────────────────────────────────────


def identity(tenant_id: uuid.UUID, runner_id: uuid.UUID) -> str:
    """What a runner puts in X-Runner-Id. It names the tenant, like every credential here."""
    return f"{tenant_id.hex}.{runner_id.hex}"


KEY_IDENTITY = "key:"


def key_identity(public_key_text: str) -> str:
    """What a runner puts in X-Runner-Id when it was not given an identity: its own key.

    A deployment that serves one tenant finds the runner by it. Where several
    tenants share a control plane the runner is given `identity()` instead.
    """
    return KEY_IDENTITY + public_key_text


def parse_identity(text: str) -> Optional[Tuple[uuid.UUID, uuid.UUID]]:
    tenant_hex, _, runner_hex = (text or "").partition(".")
    if len(tenant_hex) != 32 or len(runner_hex) != 32:
        return None
    try:
        return uuid.UUID(hex=tenant_hex), uuid.UUID(hex=runner_hex)
    except ValueError:
        return None


def request_message(method: str, path: str, timestamp: str, body: bytes) -> bytes:
    """What a runner signs for one request."""
    return "\n".join([method.upper(), path, timestamp, hashlib.sha256(body).hexdigest()]).encode(
        "utf-8"
    )


# ── The call ──────────────────────────────────────────────────────────


def envelope(
    *,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    kind: str,
    connector_id: str,
    capability_id: str,
    parameters: Mapping[str, Any],
    run_id: str,
    step_id: str,
    purpose: str,
    config: Mapping[str, Any],
    secret_refs: Mapping[str, str],
    grant: Optional[Mapping[str, Any]],
    issued_at: float,
    expires_at: float,
) -> str:
    """The text of one call. The control plane signs it; the runner checks it and nothing else."""
    if kind not in KINDS:
        raise ValueError(f"Unknown kind of call: {kind}")
    return canonical(
        {
            "v": 1,
            "call_id": str(call_id),
            "tenant_id": str(tenant_id),
            "kind": kind,
            "connector_id": connector_id,
            "capability_id": capability_id,
            "parameters": dict(parameters),
            "context": {"run_id": run_id, "step_id": step_id, "purpose": purpose},
            # Where the target is and where its credentials are kept. Never a credential.
            "connection": {"config": dict(config), "secret_refs": dict(secret_refs)},
            "grant": dict(grant) if grant else None,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
    )


# ── The result ────────────────────────────────────────────────────────


def result_to_wire(result: AdapterResult) -> Dict[str, Any]:
    """A connector's result as it is sent back. Raises ValueError if its data is too large."""
    data = result.data if isinstance(result.data, dict) else {}
    if len(canonical(data)) > MAX_DATA:
        raise ValueError("the result is too large to return")
    return {
        "success": bool(result.success),
        "data": json.loads(canonical(data)),
        "raw_output": str(result.raw_output or "")[:MAX_RAW_OUTPUT],
        "error": str(result.error or "")[:MAX_ERROR],
        "evidence_snippet": str(result.evidence_snippet or "")[:MAX_EVIDENCE],
    }


def result_from_wire(data: Any) -> AdapterResult:
    """A result a runner returned. Anything that is not one is a failed call, never a success."""
    if not isinstance(data, dict) or not isinstance(data.get("success"), bool):
        return AdapterResult(success=False, error="the runner returned no usable result")
    payload = data.get("data")
    return AdapterResult(
        success=data["success"],
        data=payload if isinstance(payload, dict) else {},
        raw_output=str(data.get("raw_output") or "")[:MAX_RAW_OUTPUT],
        error=str(data.get("error") or "")[:MAX_ERROR],
        evidence_snippet=str(data.get("evidence_snippet") or "")[:MAX_EVIDENCE],
    )


def checks_to_wire(checks: List[Any]) -> List[Dict[str, Any]]:
    return [
        {"name": str(c.name)[:200], "ok": bool(c.ok), "detail": str(c.detail)[:500]} for c in checks
    ]
