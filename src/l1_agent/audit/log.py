"""Tamper-evident audit log: one hash chain per tenant (ADR 0007).

An event is appended in the same transaction as the change it describes, so a
state change without its audit event cannot commit. Each event's hash covers
its own content and the previous event's hash. `verify` recomputes the chain.

A chain proves internal consistency. Removing the newest events, or rewriting
the whole chain, is only detectable against a checkpoint held somewhere the
database administrator cannot change; `checkpoint` produces one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from src.l1_agent.db.engine import Database
from src.l1_agent.db.models import audit_events
from src.l1_agent.utils.logging import redact_text

GENESIS_HASH = "0" * 64
MAX_TEXT = 2000


@dataclass(frozen=True)
class Actor:
    """Who caused an event: a signed-in user, an API token, or a system component."""

    type: str  # user | token | system
    id: str

    @classmethod
    def system(cls, component: str) -> "Actor":
        return cls("system", component)


def canonical(value: Any) -> str:
    """Deterministic JSON: sorted keys, no spaces, ASCII only."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def digest(value: Any) -> str:
    """SHA-256 of a value's canonical form, for payloads that carry a digest, not the data."""
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def clean(value: Any) -> Any:
    """Redact and bound free text before it enters the chain, where it cannot be removed."""
    if isinstance(value, str):
        return redact_text(value)[:MAX_TEXT]
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (uuid.UUID, datetime)):
        return str(value)
    return value


def event_hash(
    tenant_id: uuid.UUID,
    seq: int,
    occurred_at: str,
    actor_type: str,
    actor_id: str,
    correlation_id: str,
    event_type: str,
    payload: str,
    prev_hash: str,
) -> str:
    body = canonical(
        {
            "tenant_id": str(tenant_id),
            "seq": seq,
            "occurred_at": occurred_at,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "correlation_id": correlation_id,
            "event_type": event_type,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(body.encode("ascii")).hexdigest()


async def append(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    actor: Actor,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    correlation_id: str = "",
) -> int:
    """Append one event to the tenant's chain inside the caller's transaction."""
    if conn.dialect.name == "postgresql":
        # One appender per chain at a time, so seq and prev_hash are consistent.
        await conn.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"audit:{tenant_id}"},
        )
    head = (
        await conn.execute(
            sa.select(audit_events.c.seq, audit_events.c.hash)
            .where(audit_events.c.tenant_id == tenant_id)
            .order_by(audit_events.c.seq.desc())
            .limit(1)
        )
    ).first()
    seq = (head.seq + 1) if head else 1
    prev_hash = head.hash if head else GENESIS_HASH
    occurred_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    payload_text = canonical(clean(payload or {}))
    correlation_id = str(correlation_id or "")
    row_hash = event_hash(
        tenant_id,
        seq,
        occurred_at,
        actor.type,
        actor.id,
        correlation_id,
        event_type,
        payload_text,
        prev_hash,
    )
    await conn.execute(
        sa.insert(audit_events).values(
            tenant_id=tenant_id,
            seq=seq,
            occurred_at=occurred_at,
            actor_type=actor.type,
            actor_id=actor.id,
            correlation_id=correlation_id,
            event_type=event_type,
            payload=payload_text,
            prev_hash=prev_hash,
            hash=row_hash,
        )
    )
    return seq


# ── Verification ──────────────────────────────────────────────────────


@dataclass
class VerifyResult:
    tenant_id: uuid.UUID
    events_checked: int = 0
    head_seq: int = 0
    head_hash: str = GENESIS_HASH
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def sign_checkpoint(key: str, tenant_id: uuid.UUID, seq: int, head_hash: str) -> str:
    message = f"{tenant_id}:{seq}:{head_hash}".encode("ascii")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


async def checkpoint(db: Database, tenant_id: uuid.UUID, key: str) -> Dict[str, Any]:
    """The signed head of the chain. Store it outside the database."""
    result = await verify(db, tenant_id)
    if not result.ok:
        raise RuntimeError("Refusing to checkpoint a chain that does not verify")
    return {
        "tenant_id": str(tenant_id),
        "seq": result.head_seq,
        "hash": result.head_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "signature": sign_checkpoint(key, tenant_id, result.head_seq, result.head_hash),
    }


async def verify(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    checkpoints: Iterable[Dict[str, Any]] = (),
    checkpoint_key: str = "",
) -> VerifyResult:
    """Recompute the whole chain and compare it against any checkpoints given.

    Detects a changed event, a removed event, a reordered or renumbered event
    and, with a checkpoint, a chain cut short or replaced.
    """
    result = VerifyResult(tenant_id)
    hashes: Dict[int, str] = {}
    async with db.tenant(tenant_id) as conn:
        rows = await conn.stream(
            sa.select(audit_events)
            .where(audit_events.c.tenant_id == tenant_id)
            .order_by(audit_events.c.seq)
        )
        expected_seq = 1
        prev_hash = GENESIS_HASH
        async for row in rows:
            result.events_checked += 1
            if row.seq != expected_seq:
                result.problems.append(
                    f"sequence gap: expected {expected_seq}, found {row.seq} (events removed)"
                )
            if row.prev_hash != prev_hash:
                result.problems.append(f"event {row.seq}: does not follow the event before it")
            recomputed = event_hash(
                tenant_id,
                row.seq,
                row.occurred_at,
                row.actor_type,
                row.actor_id,
                row.correlation_id,
                row.event_type,
                row.payload,
                row.prev_hash,
            )
            if recomputed != row.hash:
                result.problems.append(f"event {row.seq}: content does not match its hash")
            hashes[row.seq] = row.hash
            prev_hash = row.hash
            expected_seq = row.seq + 1
            result.head_seq, result.head_hash = row.seq, row.hash

    for cp in checkpoints:
        if str(cp.get("tenant_id")) != str(tenant_id):
            continue
        seq, cp_hash = int(cp.get("seq", 0)), str(cp.get("hash", ""))
        if checkpoint_key:
            expected = sign_checkpoint(checkpoint_key, tenant_id, seq, cp_hash)
            if not hmac.compare_digest(expected, str(cp.get("signature", ""))):
                result.problems.append(f"checkpoint at {seq}: signature is not valid")
                continue
        if seq == 0:
            continue
        if seq not in hashes:
            result.problems.append(f"checkpoint at {seq}: the chain no longer reaches this event")
        elif hashes[seq] != cp_hash:
            result.problems.append(f"checkpoint at {seq}: the chain was rewritten")
    return result


async def events_for(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    correlation_id: str = "",
    after_seq: int = 0,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Audit events, oldest first. With a correlation id, one run's full timeline."""
    query = (
        sa.select(audit_events)
        .where(audit_events.c.tenant_id == tenant_id, audit_events.c.seq > after_seq)
        .order_by(audit_events.c.seq)
        .limit(limit)
    )
    if correlation_id:
        query = query.where(audit_events.c.correlation_id == correlation_id)
    async with db.tenant(tenant_id) as conn:
        rows = (await conn.execute(query)).all()
    return [
        {
            "seq": r.seq,
            "occurred_at": r.occurred_at,
            "actor": {"type": r.actor_type, "id": r.actor_id},
            "correlation_id": r.correlation_id,
            "event_type": r.event_type,
            "payload": json.loads(r.payload),
            "hash": r.hash,
        }
        for r in rows
    ]
