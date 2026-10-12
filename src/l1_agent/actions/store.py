"""Policy, kill switches and action records in the database (ADR 0008).

Every change here is made in one transaction with its audit event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from src.l1_agent.actions.policy import (
    Autonomy,
    PolicyError,
    Rules,
    Verdict,
    parse_rules,
)
from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.db.models import (
    FLAG_KILL_SWITCH,
    actions,
    incidents,
    jobs,
    platform_flags,
    policies,
    runs,
    sop_versions,
    sops,
    users,
)

ENGINE = Actor.system("policy-engine")
MAX_NOTE = 1000

SWITCH_GLOBAL = "global"
SWITCH_TENANT = "tenant"
SWITCH_LOCAL = "local"

# Statuses in which the step is finished and its result is stored on the row.
FINAL = frozenset(
    {
        "denied",
        "shadowed",
        "recommended",
        "rejected",
        "expired",
        "invalid",
        "not_done",
        "verified",
        "failed",
        "rolled_back",
        "rollback_failed",
        "aborted",
    }
)
VERDICT_STATUS = {
    Verdict.DENY: "denied",
    Verdict.SHADOW: "shadowed",
    Verdict.RECOMMEND: "recommended",
    Verdict.REQUIRE_APPROVAL: "pending",
    Verdict.ALLOW: "allowed",
}


class ActionError(Exception):
    """A request about an action or the policy cannot be carried out. Carries an HTTP status."""

    status = 400

    def __init__(self, message: str, problems: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.problems = problems or []


class NotFound(ActionError):
    status = 404


class WrongState(ActionError):
    status = 409


class NotAllowed(ActionError):
    status = 403


def who(actor: Actor) -> str:
    return f"{actor.type}:{actor.id}"


# ── Policy ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TenantPolicy:
    autonomy: Autonomy
    rules: Rules
    raw_rules: Mapping[str, Any]
    kill_switch: bool
    kill_reason: str = ""
    kill_set_by: str = ""
    kill_set_at: Optional[datetime] = None
    updated_by: str = ""
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "autonomy": self.autonomy.value,
            "rules": dict(self.raw_rules),
            "kill_switch": {
                "on": self.kill_switch,
                "reason": self.kill_reason,
                "set_by": self.kill_set_by,
                "set_at": self.kill_set_at.isoformat() if self.kill_set_at else None,
            },
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


DEFAULT_POLICY = TenantPolicy(Autonomy.SHADOW, Rules(), {}, False)


def _policy(row: Any) -> TenantPolicy:
    if row is None:
        return DEFAULT_POLICY
    try:
        rules = parse_rules(row.rules)
        autonomy = Autonomy(row.autonomy)
    except (PolicyError, ValueError):
        # Rules that no longer parse must not be read as "no rules". Shadow does nothing.
        rules, autonomy = Rules(), Autonomy.SHADOW
    return TenantPolicy(
        autonomy,
        rules,
        row.rules or {},
        bool(row.kill_switch),
        row.kill_reason,
        row.kill_set_by,
        row.kill_set_at,
        row.updated_by,
        row.updated_at,
    )


async def get_policy(db: Database, tenant_id: uuid.UUID) -> TenantPolicy:
    """The tenant's policy. A tenant that has never set one is in shadow with no rules."""
    async with db.tenant(tenant_id) as conn:
        return await _read_policy(conn, tenant_id)


async def _read_policy(conn: AsyncConnection, tenant_id: uuid.UUID) -> TenantPolicy:
    row = (await conn.execute(sa.select(policies).where(policies.c.tenant_id == tenant_id))).first()
    return _policy(row)


async def _upsert_policy(
    conn: AsyncConnection, tenant_id: uuid.UUID, actor: Actor, values: Dict[str, Any]
) -> None:
    now = utcnow()
    found = (
        await conn.execute(sa.select(policies.c.tenant_id).where(policies.c.tenant_id == tenant_id))
    ).first()
    if found is None:
        await conn.execute(
            sa.insert(policies).values(
                tenant_id=tenant_id,
                autonomy=Autonomy.SHADOW.value,
                rules={},
                updated_by=who(actor),
                updated_at=now,
            )
        )
    await conn.execute(
        sa.update(policies).where(policies.c.tenant_id == tenant_id).values(**values)
    )


async def set_policy(
    db: Database,
    tenant_id: uuid.UUID,
    actor: Actor,
    *,
    autonomy: Optional[str] = None,
    rules: Optional[Mapping[str, Any]] = None,
) -> TenantPolicy:
    """Change the autonomy level, the rules, or both. Invalid rules change nothing."""
    values: Dict[str, Any] = {"updated_by": who(actor), "updated_at": utcnow()}
    if autonomy is not None:
        try:
            values["autonomy"] = Autonomy(autonomy).value
        except ValueError:
            raise ActionError(
                "autonomy must be one of " + ", ".join(a.value for a in Autonomy)
            ) from None
    if rules is not None:
        try:
            parse_rules(rules)
        except PolicyError as exc:
            raise ActionError("The rules are not valid", exc.problems) from None
        values["rules"] = dict(rules)
    async with db.tenant(tenant_id) as conn:
        before = await _read_policy(conn, tenant_id)
        await _upsert_policy(conn, tenant_id, actor, values)
        await audit.append(
            conn,
            tenant_id,
            actor,
            "policy.changed",
            {
                "autonomy_before": before.autonomy.value,
                "autonomy": values.get("autonomy", before.autonomy.value),
                "rules_changed": rules is not None,
                "rules_sha256": audit.digest(values.get("rules", before.raw_rules)),
            },
        )
        return await _read_policy(conn, tenant_id)


# ── Kill switches ─────────────────────────────────────────────────────


async def set_kill_switch(
    db: Database, tenant_id: uuid.UUID, actor: Actor, on: bool, reason: str = ""
) -> None:
    """Engage or release the tenant's kill switch."""
    now = utcnow()
    async with db.tenant(tenant_id) as conn:
        await _upsert_policy(
            conn,
            tenant_id,
            actor,
            {
                "kill_switch": on,
                "kill_reason": reason[:MAX_NOTE],
                "kill_set_by": who(actor),
                "kill_set_at": now,
            },
        )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "killswitch.engaged" if on else "killswitch.released",
            {"scope": SWITCH_TENANT, "reason": reason[:MAX_NOTE]},
        )


async def set_global_kill_switch(owner: Database, actor: Actor, on: bool, reason: str = "") -> int:
    """Engage or release the switch for every tenant. Needs the migration role.

    The event is written to every tenant's audit log. Returns how many tenants there are.
    """
    now = utcnow()
    async with owner.platform() as conn:
        found = (
            await conn.execute(
                sa.select(platform_flags.c.name).where(platform_flags.c.name == FLAG_KILL_SWITCH)
            )
        ).first()
        values = dict(enabled=on, reason=reason[:MAX_NOTE], set_by=who(actor), set_at=now)
        if found is None:
            await conn.execute(sa.insert(platform_flags).values(name=FLAG_KILL_SWITCH, **values))
        else:
            await conn.execute(
                sa.update(platform_flags)
                .where(platform_flags.c.name == FLAG_KILL_SWITCH)
                .values(**values)
            )
    tenant_ids = await owner.active_tenants()
    for tenant_id in tenant_ids:
        async with owner.tenant(tenant_id) as conn:
            await audit.append(
                conn,
                tenant_id,
                actor,
                "killswitch.engaged" if on else "killswitch.released",
                {"scope": SWITCH_GLOBAL, "reason": reason[:MAX_NOTE]},
            )
    return len(tenant_ids)


async def global_kill_switch(db: Database) -> Dict[str, Any]:
    async with db.platform() as conn:
        row = (
            await conn.execute(
                sa.select(platform_flags).where(platform_flags.c.name == FLAG_KILL_SWITCH)
            )
        ).first()
    if row is None:
        return {"on": False, "reason": "", "set_by": "", "set_at": None}
    return {
        "on": bool(row.enabled),
        "reason": row.reason,
        "set_by": row.set_by,
        "set_at": row.set_at.isoformat() if row.set_at else None,
    }


async def _switches_on(conn: AsyncConnection, tenant_id: uuid.UUID) -> Tuple[str, ...]:
    """The database switches that are on, read now. There is no cache."""
    on: List[str] = []
    if (
        await conn.execute(
            sa.select(platform_flags.c.enabled).where(platform_flags.c.name == FLAG_KILL_SWITCH)
        )
    ).scalar():
        on.append(SWITCH_GLOBAL)
    if (
        await conn.execute(
            sa.select(policies.c.kill_switch).where(policies.c.tenant_id == tenant_id)
        )
    ).scalar():
        on.append(SWITCH_TENANT)
    return tuple(on)


async def switches_on(db: Database, tenant_id: uuid.UUID) -> Tuple[str, ...]:
    async with db.tenant(tenant_id) as conn:
        return await _switches_on(conn, tenant_id)


# ── Action records ────────────────────────────────────────────────────


def describe(row: Any) -> Dict[str, Any]:
    """An action as the approver and the dashboard see it."""

    def when(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None

    return {
        "id": str(row.id),
        "run_id": str(row.run_id),
        "incident_number": row.incident_number,
        "step_id": row.step_id,
        "sop_id": row.sop_key,
        "sop_version": row.sop_version,
        "sop_hash": row.sop_hash,
        "sop_author": row.sop_author_id,
        "capability": row.capability_id,
        "parameters": row.parameters,
        "action_hash": row.action_hash,
        "tier": row.tier,
        "verdict": row.verdict,
        "reason_code": row.reason_code,
        "rule": row.rule,
        "status": row.status,
        "change_number": row.change_number,
        "channels": list(row.channels or []),
        "requested_at": when(row.requested_at),
        "expires_at": when(row.expires_at),
        "approver": row.approver_id,
        "decided_via": row.decided_via,
        "decision_note": row.decision_note,
        "decided_at": when(row.decided_at),
        "executed_at": when(row.execution_started_at),
        "completed_at": when(row.completed_at),
        "created_at": when(row.created_at),
    }


async def find(db: Database, tenant_id: uuid.UUID, run_id: uuid.UUID, step_id: str) -> Any:
    async with db.tenant(tenant_id) as conn:
        return (
            await conn.execute(
                sa.select(actions).where(
                    actions.c.tenant_id == tenant_id,
                    actions.c.run_id == run_id,
                    actions.c.step_id == step_id,
                )
            )
        ).first()


async def get(db: Database, tenant_id: uuid.UUID, action_id: uuid.UUID) -> Any:
    async with db.tenant(tenant_id) as conn:
        row = await _row(conn, tenant_id, action_id, lock=False)
    return row


async def _row(conn: AsyncConnection, tenant_id: uuid.UUID, action_id: uuid.UUID, lock=True):
    query = sa.select(actions).where(actions.c.tenant_id == tenant_id, actions.c.id == action_id)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).first()
    if row is None:
        raise NotFound("Not found")
    return row


async def list_actions(
    db: Database, tenant_id: uuid.UUID, *, status: str = "", run_id: str = "", limit: int = 100
) -> List[Dict[str, Any]]:
    query = (
        sa.select(actions)
        .where(actions.c.tenant_id == tenant_id)
        .order_by(actions.c.created_at.desc())
        .limit(min(500, max(1, limit)))
    )
    if status:
        query = query.where(actions.c.status == status)
    if run_id:
        try:
            query = query.where(actions.c.run_id == uuid.UUID(run_id))
        except ValueError:
            return []
    async with db.tenant(tenant_id) as conn:
        return [describe(r) for r in await conn.execute(query)]


async def sop_author(
    conn: AsyncConnection, tenant_id: uuid.UUID, sop_key: str, content_hash: str
) -> str:
    """Who wrote the SOP version that is running. Empty when it is not from the version store."""
    if not content_hash:
        return ""
    found = await conn.execute(
        sa.select(sop_versions.c.author_id)
        .join(sops, sops.c.id == sop_versions.c.sop_id)
        .where(
            sop_versions.c.tenant_id == tenant_id,
            sops.c.sop_key == sop_key,
            sop_versions.c.content_hash == content_hash,
            sop_versions.c.version.is_not(None),
        )
        .limit(1)
    )
    return found.scalar() or ""


async def author_email(conn: AsyncConnection, tenant_id: uuid.UUID, author_id: str) -> str:
    """The email of an SOP author who signed in through the identity provider, if known."""
    kind, _, identity = author_id.partition(":")
    issuer, sep, subject = identity.partition("|")
    if kind != "user" or not sep:
        return ""
    found = await conn.execute(
        sa.select(users.c.email).where(
            users.c.tenant_id == tenant_id, users.c.issuer == issuer, users.c.subject == subject
        )
    )
    return (found.scalar() or "").strip().lower()


async def recent_executions(conn: AsyncConnection, tenant_id: uuid.UUID, minutes: int) -> int:
    found = await conn.execute(
        sa.select(sa.func.count())
        .select_from(actions)
        .where(
            actions.c.tenant_id == tenant_id,
            actions.c.execution_started_at >= utcnow() - timedelta(minutes=minutes),
        )
    )
    return found.scalar() or 0


async def record_decision(
    db: Database, tenant_id: uuid.UUID, values: Dict[str, Any], audit_payload: Dict[str, Any]
) -> Any:
    """Write the decision and its audit event. Returns the row, or the one already there."""
    now = utcnow()
    try:
        async with db.tenant(tenant_id) as conn:
            incident_id = (
                await conn.execute(
                    sa.select(runs.c.incident_id).where(
                        runs.c.tenant_id == tenant_id, runs.c.id == values["run_id"]
                    )
                )
            ).scalar()
            action_id = uuid.uuid4()
            await conn.execute(
                sa.insert(actions).values(
                    id=action_id,
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    created_at=now,
                    updated_at=now,
                    **values,
                )
            )
            await audit.append(
                conn,
                tenant_id,
                ENGINE,
                "policy.decided",
                {"action_id": str(action_id), **audit_payload},
                correlation_id=str(values["run_id"]),
            )
            return await _row(conn, tenant_id, action_id, lock=False)
    except IntegrityError:
        # Another attempt at this step recorded its decision first. That one stands.
        return await find(db, tenant_id, values["run_id"], values["step_id"])


async def transition(
    db: Database,
    tenant_id: uuid.UUID,
    action_id: uuid.UUID,
    *,
    from_statuses: Tuple[str, ...],
    to_status: str,
    event: str,
    payload: Optional[Dict[str, Any]] = None,
    values: Optional[Dict[str, Any]] = None,
    actor: Actor = ENGINE,
    wake: bool = False,
) -> Any:
    """Move an action from one of `from_statuses` to `to_status`, with its audit event.

    Returns the updated row, or None when the action was not in one of those
    statuses (someone else moved it first); nothing is written in that case.
    """
    now = utcnow()
    async with db.tenant(tenant_id) as conn:
        changed = await conn.execute(
            sa.update(actions)
            .where(
                actions.c.tenant_id == tenant_id,
                actions.c.id == action_id,
                actions.c.status.in_(from_statuses),
            )
            .values(
                status=to_status,
                updated_at=now,
                **({"completed_at": now} if to_status in FINAL else {}),
                **(values or {}),
            )
        )
        if not changed.rowcount:
            return None
        row = await _row(conn, tenant_id, action_id, lock=False)
        await audit.append(
            conn,
            tenant_id,
            actor,
            event,
            {
                "action_id": str(action_id),
                "step_id": row.step_id,
                "capability": row.capability_id,
                "action_sha256": row.action_hash,
                "status": to_status,
                **(payload or {}),
            },
            correlation_id=str(row.run_id),
        )
        if wake:
            await wake_run(conn, tenant_id, row.run_id)
        return row


async def note(
    db: Database, tenant_id: uuid.UUID, row: Any, event: str, payload: Dict[str, Any], **values: Any
) -> Any:
    """Record something about an action that does not change its status."""
    async with db.tenant(tenant_id) as conn:
        if values:
            await conn.execute(
                sa.update(actions)
                .where(actions.c.tenant_id == tenant_id, actions.c.id == row.id)
                .values(updated_at=utcnow(), **values)
            )
        await audit.append(
            conn,
            tenant_id,
            ENGINE,
            event,
            {"action_id": str(row.id), "step_id": row.step_id, **payload},
            correlation_id=str(row.run_id),
        )
        return await _row(conn, tenant_id, row.id, lock=False)


async def authorise_execution(
    db: Database, tenant_id: uuid.UUID, action_id: uuid.UUID, local_switch: bool
) -> Tuple[Any, str]:
    """The one step that lets a write go ahead. Returns (row, "") or (None, why not).

    In one transaction: the kill switches are read, and the row moves from
    `approved` or `allowed` to `executing`. If the row is in any other status
    nothing changes. The database constraints on `actions` apply to this
    update, so a row that was never authorised cannot be moved.
    """
    now = utcnow()
    try:
        async with db.tenant(tenant_id) as conn:
            on = list(await _switches_on(conn, tenant_id)) + (
                [SWITCH_LOCAL] if local_switch else []
            )
            if on:
                return None, "kill switch is on: " + ", ".join(on)
            changed = await conn.execute(
                sa.update(actions)
                .where(
                    actions.c.tenant_id == tenant_id,
                    actions.c.id == action_id,
                    actions.c.status.in_(("approved", "allowed")),
                    actions.c.valid_until > now,
                )
                .values(status="executing", execution_started_at=now, updated_at=now)
            )
            if not changed.rowcount:
                return None, "the action is no longer authorised"
            row = await _row(conn, tenant_id, action_id, lock=False)
            await audit.append(
                conn,
                tenant_id,
                ENGINE,
                "action.executing",
                {
                    "action_id": str(action_id),
                    "step_id": row.step_id,
                    "capability": row.capability_id,
                    "parameters": row.parameters,
                    "action_sha256": row.action_hash,
                    "tier": row.tier,
                    "authorised_by": row.approver_id or "policy",
                    "change": row.change_number,
                },
                correlation_id=str(row.run_id),
            )
            return row, ""
    except IntegrityError:
        return None, "this action has already been carried out for this incident"


# ── Approval decisions ────────────────────────────────────────────────


async def decide(
    db: Database,
    tenant_id: uuid.UUID,
    actor: Actor,
    action_id: uuid.UUID,
    *,
    approve: bool,
    seen_hash: str,
    note_text: str = "",
    via: str = "ui",
    approver_email: str = "",
) -> Dict[str, Any]:
    """Approve or reject a pending action, exactly as the approver saw it.

    For T2 the approver must be someone other than the author of the SOP
    version. The database constraints repeat the rule.
    """
    now = utcnow()
    expired = False
    async with db.tenant(tenant_id) as conn:
        row = await _row(conn, tenant_id, action_id)
        if row.status != "pending":
            raise WrongState(f"This action is not waiting for approval (it is {row.status})")
        if not seen_hash or seen_hash != row.action_hash:
            raise WrongState("The action is not the one that was shown; look at it again")
        if row.expires_at is None or now >= row.expires_at:
            expired = True
        elif approve and row.tier == "T2":
            if not row.sop_author_id:
                raise NotAllowed("The author of the SOP is not on record; a T2 action needs one")
            if row.sop_author_id == who(actor):
                raise NotAllowed("The author of the SOP cannot approve a T2 action from it")
        if expired:
            status, event, extra = "expired", "approval.expired", {}
        else:
            status = "approved" if approve else "rejected"
            event = "approval.decided"
            policy = await _read_policy(conn, tenant_id)
            extra = dict(
                approver_id=who(actor),
                approver_email=approver_email[:320],
                approved_hash=row.action_hash if approve else "",
                decided_via=via,
                decision_note=note_text[:MAX_NOTE],
                decided_at=now,
                valid_until=now + timedelta(minutes=policy.rules.execute_within_minutes),
            )
        await conn.execute(
            sa.update(actions)
            .where(actions.c.tenant_id == tenant_id, actions.c.id == action_id)
            .values(
                status=status,
                updated_at=now,
                **({} if approve and not expired else {"completed_at": now}),
                **extra,
            )
        )
        await audit.append(
            conn,
            tenant_id,
            ENGINE if expired else actor,
            event,
            {
                "action_id": str(action_id),
                "step_id": row.step_id,
                "capability": row.capability_id,
                "action_sha256": row.action_hash,
                "decision": status,
                "via": via,
                "tier": row.tier,
                "note": note_text[:MAX_NOTE],
            },
            correlation_id=str(row.run_id),
        )
        await wake_run(conn, tenant_id, row.run_id)
        after = describe(await _row(conn, tenant_id, action_id, lock=False))
    if expired:
        raise WrongState("The approval request has expired")
    return after


async def pending(db: Database, tenant_id: uuid.UUID) -> List[Any]:
    async with db.tenant(tenant_id) as conn:
        return list(
            await conn.execute(
                sa.select(actions)
                .where(actions.c.tenant_id == tenant_id, actions.c.status == "pending")
                .order_by(actions.c.created_at)
            )
        )


async def wake_run(conn: AsyncConnection, tenant_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Put a run that is parked waiting for an approval back on the queue."""
    await conn.execute(
        sa.update(jobs)
        .where(jobs.c.tenant_id == tenant_id, jobs.c.run_id == run_id, jobs.c.status == "waiting")
        .values(status="queued", available_at=utcnow(), attempts=0, locked_until=None)
    )


async def incident_payload(db: Database, tenant_id: uuid.UUID, run_id: uuid.UUID) -> Dict[str, Any]:
    async with db.tenant(tenant_id) as conn:
        found = await conn.execute(
            sa.select(incidents.c.payload)
            .join(runs, runs.c.incident_id == incidents.c.id)
            .where(runs.c.tenant_id == tenant_id, runs.c.id == run_id)
        )
        return found.scalar() or {}
