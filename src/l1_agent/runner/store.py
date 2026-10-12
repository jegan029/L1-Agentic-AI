"""Enrolled runners and the queue of calls to them (ADR 0010).

A tenant administrator enrols a runner by giving its public key a name. From
then on that runner may claim the tenant's calls and return their results.
Revoking a runner stops both at once: it is checked on every request.

The queue is a table. A call is `queued` until a runner claims it, `claimed`
until the runner returns a result, then `done`. The caller removes the row
when it has read the result; a call nobody claims in time is `cancelled`,
and a cancelled call can never be claimed.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import sqlalchemy as sa

from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.config.settings import ConfigError
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.db.models import runner_calls, runners
from src.l1_agent.runner import keys, protocol

# A runner that has asked for work this recently is counted as connected.
CONNECTED_SECONDS = 90
# How long an abandoned row is kept before any enqueue removes it.
STALE = timedelta(minutes=30)


class RunnerError(ValueError):
    """An enrolment that cannot be made."""


@dataclass(frozen=True)
class RunnerIdentity:
    tenant_id: uuid.UUID
    id: uuid.UUID
    name: str
    public_key: bytes

    @property
    def actor(self) -> Actor:
        return Actor("runner", f"{self.name}|{self.id}")


@dataclass(frozen=True)
class Claimed:
    id: uuid.UUID
    envelope: str
    signature: str
    is_write: bool


def fingerprint(public_key_text: str) -> str:
    """A short name for a public key, for the audit log and for a person to compare."""
    return hashlib.sha256(public_key_text.encode("ascii")).hexdigest()[:16]


# ── Enrolment ─────────────────────────────────────────────────────────


async def enrol(
    db: Database, tenant_id: uuid.UUID, actor: Actor, name: str, public_key: str
) -> uuid.UUID:
    """Let the holder of this key's private half work for the tenant."""
    name = (name or "").strip()
    if not name or len(name) > 100:
        raise RunnerError("A runner's name is 1 to 100 characters")
    try:
        text = keys.public_text(keys.parse_public(public_key))
    except ConfigError as exc:
        raise RunnerError(str(exc)) from None
    runner_id = uuid.uuid4()
    async with db.tenant(tenant_id) as conn:
        taken = (
            await conn.execute(
                sa.select(runners.c.id).where(
                    runners.c.tenant_id == tenant_id, runners.c.name == name
                )
            )
        ).first()
        if taken:
            raise RunnerError(f"A runner called '{name}' is already enrolled")
        same_key = (
            await conn.execute(
                sa.select(runners.c.name).where(
                    runners.c.tenant_id == tenant_id, runners.c.public_key == text
                )
            )
        ).first()
        if same_key:
            raise RunnerError(
                f"That key is already enrolled as '{same_key.name}'; each runner has its own key"
            )
        await conn.execute(
            sa.insert(runners).values(
                id=runner_id,
                tenant_id=tenant_id,
                name=name,
                public_key=text,
                enrolled_by=f"{actor.type}:{actor.id}"[:300],
                created_at=utcnow(),
            )
        )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "runner.enrolled",
            {"runner_id": str(runner_id), "name": name, "key_fingerprint": fingerprint(text)},
        )
    return runner_id


async def revoke(db: Database, tenant_id: uuid.UUID, actor: Actor, runner_id: uuid.UUID) -> bool:
    async with db.tenant(tenant_id) as conn:
        changed = await conn.execute(
            sa.update(runners)
            .where(
                runners.c.tenant_id == tenant_id,
                runners.c.id == runner_id,
                runners.c.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        if changed.rowcount:
            await audit.append(
                conn, tenant_id, actor, "runner.revoked", {"runner_id": str(runner_id)}
            )
    return bool(changed.rowcount)


async def list_runners(db: Database, tenant_id: uuid.UUID) -> List[Dict[str, Any]]:
    now = utcnow()

    def when(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None

    async with db.tenant(tenant_id) as conn:
        rows = await conn.execute(
            sa.select(runners).where(runners.c.tenant_id == tenant_id).order_by(runners.c.name)
        )
        return [
            {
                "id": str(r.id),
                "identity": protocol.identity(tenant_id, r.id),
                "name": r.name,
                "key_fingerprint": fingerprint(r.public_key),
                "enrolled_by": r.enrolled_by,
                "created_at": when(r.created_at),
                "revoked": r.revoked_at is not None,
                "last_seen_at": when(r.last_seen_at),
                "connected": bool(
                    r.revoked_at is None
                    and r.last_seen_at
                    and now - r.last_seen_at < timedelta(seconds=CONNECTED_SECONDS)
                ),
                "reported": r.reported or {},
            }
            for r in rows
        ]


async def connected_count(db: Database, tenant_id: uuid.UUID) -> int:
    return sum(1 for r in await list_runners(db, tenant_id) if r["connected"])


async def find(
    db: Database, tenant_id: uuid.UUID, runner_id: uuid.UUID
) -> Optional[RunnerIdentity]:
    """An enrolled runner that has not been revoked. Read on every request; no cache."""
    async with db.tenant(tenant_id) as conn:
        row = (
            await conn.execute(
                sa.select(runners.c.name, runners.c.public_key).where(
                    runners.c.tenant_id == tenant_id,
                    runners.c.id == runner_id,
                    runners.c.revoked_at.is_(None),
                )
            )
        ).first()
    if row is None:
        return None
    try:
        return RunnerIdentity(tenant_id, runner_id, row.name, keys.parse_public(row.public_key))
    except ConfigError:
        return None


async def find_by_key(
    db: Database, tenant_id: uuid.UUID, public_key: str
) -> Optional[RunnerIdentity]:
    """The tenant's runner that was enrolled with this key, unless it has been revoked."""
    try:
        text = keys.public_text(keys.parse_public(public_key))
    except ConfigError:
        return None
    async with db.tenant(tenant_id) as conn:
        row = (
            await conn.execute(
                sa.select(runners.c.id, runners.c.name).where(
                    runners.c.tenant_id == tenant_id,
                    runners.c.public_key == text,
                    runners.c.revoked_at.is_(None),
                )
            )
        ).first()
    if row is None:
        return None
    return RunnerIdentity(tenant_id, row.id, row.name, keys.parse_public(text))


async def seen(db: Database, runner: RunnerIdentity, reported: Dict[str, Any]) -> None:
    async with db.tenant(runner.tenant_id) as conn:
        await conn.execute(
            sa.update(runners)
            .where(runners.c.tenant_id == runner.tenant_id, runners.c.id == runner.id)
            .values(last_seen_at=utcnow(), reported=reported)
        )


# ── The queue ─────────────────────────────────────────────────────────


async def enqueue(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    call_id: uuid.UUID,
    kind: str,
    connector_id: str,
    capability_id: str,
    is_write: bool,
    envelope: str,
    signature: str,
    deadline: datetime,
) -> None:
    now = utcnow()
    async with db.tenant(tenant_id) as conn:
        await conn.execute(
            sa.delete(runner_calls).where(
                runner_calls.c.tenant_id == tenant_id, runner_calls.c.deadline < now - STALE
            )
        )
        await conn.execute(
            sa.insert(runner_calls).values(
                id=call_id,
                tenant_id=tenant_id,
                kind=kind,
                connector_id=connector_id,
                capability_id=capability_id,
                is_write=is_write,
                envelope=envelope,
                signature=signature,
                status="queued",
                created_at=now,
                deadline=deadline,
            )
        )


async def claim(db: Database, runner: RunnerIdentity, limit: int) -> List[Claimed]:
    """Hand this runner the oldest calls that are waiting and still in time."""
    now = utcnow()
    waiting = (
        sa.select(runner_calls.c.id)
        .where(
            runner_calls.c.tenant_id == runner.tenant_id,
            runner_calls.c.status == "queued",
            runner_calls.c.deadline > now,
        )
        .order_by(runner_calls.c.created_at)
        .limit(max(1, limit))
        .with_for_update(skip_locked=True)
    )
    async with db.tenant(runner.tenant_id) as conn:
        rows = await conn.execute(
            sa.update(runner_calls)
            .where(runner_calls.c.id.in_(waiting))
            .values(status="claimed", runner_id=runner.id, claimed_at=now)
            .returning(
                runner_calls.c.id,
                runner_calls.c.envelope,
                runner_calls.c.signature,
                runner_calls.c.is_write,
                runner_calls.c.created_at,
            )
        )
        found = sorted(rows.all(), key=lambda r: r.created_at)
    return [Claimed(r.id, r.envelope, r.signature, bool(r.is_write)) for r in found]


async def record_result(
    db: Database,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    outcome: str,
    result: Dict[str, Any],
    *,
    runner_id: Optional[uuid.UUID] = None,
) -> bool:
    """Record a call's result. A runner may only answer a call it holds, and only once."""
    where = [
        runner_calls.c.tenant_id == tenant_id,
        runner_calls.c.id == call_id,
        runner_calls.c.status == "claimed",
    ]
    if runner_id is not None:
        where.append(runner_calls.c.runner_id == runner_id)
    async with db.tenant(tenant_id) as conn:
        changed = await conn.execute(
            sa.update(runner_calls)
            .where(*where)
            .values(status="done", outcome=outcome, result=result, completed_at=utcnow())
        )
    return bool(changed.rowcount)


async def cancel(db: Database, tenant_id: uuid.UUID, call_id: uuid.UUID) -> bool:
    """Withdraw a call no runner has taken. True means it was never handed to one."""
    async with db.tenant(tenant_id) as conn:
        changed = await conn.execute(
            sa.update(runner_calls)
            .where(
                runner_calls.c.tenant_id == tenant_id,
                runner_calls.c.id == call_id,
                runner_calls.c.status == "queued",
            )
            .values(status="cancelled", completed_at=utcnow())
        )
    return bool(changed.rowcount)


async def answer(db: Database, tenant_id: uuid.UUID, call_id: uuid.UUID) -> Optional[Any]:
    """The call's row once it is done; None while it is still waiting or being made."""
    async with db.tenant(tenant_id) as conn:
        row = (
            await conn.execute(
                sa.select(
                    runner_calls.c.status, runner_calls.c.outcome, runner_calls.c.result
                ).where(runner_calls.c.tenant_id == tenant_id, runner_calls.c.id == call_id)
            )
        ).first()
    return row if row is not None and row.status == "done" else None


async def remove(db: Database, tenant_id: uuid.UUID, call_id: uuid.UUID) -> None:
    async with db.tenant(tenant_id) as conn:
        await conn.execute(
            sa.delete(runner_calls).where(
                runner_calls.c.tenant_id == tenant_id, runner_calls.c.id == call_id
            )
        )
