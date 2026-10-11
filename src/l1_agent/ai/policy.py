"""A tenant's model policy and the record of its model calls (ADR 0009).

The policy says whether the tenant uses a model at all, which providers and
tasks it allows, where its data may be processed, how long a provider may keep
it, and how much may be spent. A tenant with no policy uses no model.

`permits` is pure. The store functions follow the pattern of
`actions/store.py`: every change is audited in the transaction that makes it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from src.l1_agent.ai.prompts import TASKS
from src.l1_agent.ai.providers import PROVIDER_KINDS
from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.db.models import model_calls, model_policies

DEFAULT_INCIDENT_TOKENS = 20_000
DEFAULT_DAILY_TOKENS = 1_000_000
MAX_TOKENS = 1_000_000_000
MICROS = 1_000_000

MODEL_OFF = "model_off"
TASK_NOT_ENABLED = "task_not_enabled"
PROVIDER_NOT_ALLOWED = "provider_not_allowed"
REGION_NOT_ALLOWED = "region_not_allowed"
RETENTION_NOT_ALLOWED = "retention_not_allowed"

_RULE_KEYS = frozenset({"providers", "tasks", "regions", "max_retention_days", "budgets"})
_BUDGET_KEYS = frozenset({"incident_tokens", "daily_tokens", "incident_cost_usd", "daily_cost_usd"})


class ModelPolicyError(Exception):
    """A model policy that cannot be accepted. `problems` lists every reason."""

    def __init__(self, message: str, problems: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.problems = problems or []


@dataclass(frozen=True)
class ModelPolicy:
    enabled: bool = False
    providers: Tuple[str, ...] = ()  # empty: none
    tasks: Tuple[str, ...] = ()  # empty: none
    regions: Tuple[str, ...] = ()  # empty: no restriction
    max_retention_days: Optional[int] = None  # None: no restriction
    incident_tokens: int = DEFAULT_INCIDENT_TOKENS
    daily_tokens: int = DEFAULT_DAILY_TOKENS
    incident_cost_micros: Optional[int] = None  # None: no cost limit
    daily_cost_micros: Optional[int] = None
    raw_rules: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "rules": dict(self.raw_rules)}


OFF = ModelPolicy()


def _strings(rules: Mapping[str, Any], key: str, allowed: Optional[tuple], problems: List[str]):
    value = rules.get(key, [])
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        problems.append(f"{key} must be a list of names")
        return ()
    cleaned = tuple(dict.fromkeys(v.strip().lower() for v in value))
    if allowed is not None:
        for name in cleaned:
            if name not in allowed:
                problems.append(f"{key}: '{name}' is not one of {list(allowed)}")
    return cleaned


def _whole(value: Any, label: str, problems: List[str], maximum: int = MAX_TOKENS) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        problems.append(f"{label} must be a whole number from 0 to {maximum}")
        return 0
    return value


def _cost(value: Any, label: str, problems: List[str]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1e6:
        problems.append(f"{label} must be an amount in US dollars from 0 to 1000000")
        return None
    return round(value * MICROS)


def parse_model_policy(enabled: bool, rules: Any) -> ModelPolicy:
    """Validate a policy. Raises ModelPolicyError with every problem found."""
    if not isinstance(rules, Mapping):
        raise ModelPolicyError("The model policy is not valid", ["rules must be an object"])
    problems = [f"unknown rule '{k}'" for k in rules if k not in _RULE_KEYS]
    providers = _strings(rules, "providers", PROVIDER_KINDS, problems)
    tasks = _strings(rules, "tasks", TASKS, problems)
    regions = _strings(rules, "regions", None, problems)
    retention = rules.get("max_retention_days")
    if retention is not None:
        retention = _whole(retention, "max_retention_days", problems, 36_500)
    budgets = rules.get("budgets", {})
    if not isinstance(budgets, Mapping):
        problems.append("budgets must be an object")
        budgets = {}
    problems += [f"unknown budget '{k}'" for k in budgets if k not in _BUDGET_KEYS]
    incident = _whole(
        budgets.get("incident_tokens", DEFAULT_INCIDENT_TOKENS), "incident_tokens", problems
    )
    daily = _whole(budgets.get("daily_tokens", DEFAULT_DAILY_TOKENS), "daily_tokens", problems)
    incident_cost = _cost(budgets.get("incident_cost_usd"), "incident_cost_usd", problems)
    daily_cost = _cost(budgets.get("daily_cost_usd"), "daily_cost_usd", problems)
    if enabled and not providers:
        problems.append("providers: name at least one provider to switch the model on")
    if enabled and not tasks:
        problems.append("tasks: name at least one task to switch the model on")
    if problems:
        raise ModelPolicyError("The model policy is not valid", problems)
    return ModelPolicy(
        bool(enabled),
        providers,
        tasks,
        regions,
        retention,
        incident,
        daily,
        incident_cost,
        daily_cost,
        dict(rules),
    )


def permits(policy: ModelPolicy, provider: Any, task: str) -> Optional[str]:
    """Why this tenant may not use this provider for this task, or None when it may."""
    if not policy.enabled:
        return MODEL_OFF
    if task not in policy.tasks:
        return TASK_NOT_ENABLED
    if provider.id not in policy.providers:
        return PROVIDER_NOT_ALLOWED
    if policy.regions and (provider.region or "").lower() not in policy.regions:
        return REGION_NOT_ALLOWED  # a provider with no stated region is refused too
    if policy.max_retention_days is not None and (
        provider.retention_days is None or provider.retention_days > policy.max_retention_days
    ):
        return RETENTION_NOT_ALLOWED
    return None


# ── Store ─────────────────────────────────────────────────────────────


async def read_policy(conn: AsyncConnection, tenant_id: uuid.UUID) -> ModelPolicy:
    row = (
        await conn.execute(sa.select(model_policies).where(model_policies.c.tenant_id == tenant_id))
    ).first()
    if row is None:
        return OFF
    try:
        return parse_model_policy(bool(row.enabled), row.rules or {})
    except ModelPolicyError:
        return OFF  # a stored policy this version cannot read switches the model off


async def get_model_policy(db: Database, tenant_id: uuid.UUID) -> ModelPolicy:
    """The tenant's model policy. A tenant that has never set one uses no model."""
    async with db.tenant(tenant_id) as conn:
        return await read_policy(conn, tenant_id)


async def set_model_policy(
    db: Database,
    tenant_id: uuid.UUID,
    actor: Actor,
    *,
    enabled: Optional[bool] = None,
    rules: Optional[Mapping[str, Any]] = None,
) -> ModelPolicy:
    """Switch the model on or off, change the rules, or both. An invalid policy changes nothing."""
    async with db.tenant(tenant_id) as conn:
        before = await read_policy(conn, tenant_id)
        policy = parse_model_policy(
            before.enabled if enabled is None else enabled,
            before.raw_rules if rules is None else rules,
        )
        values = {
            "enabled": policy.enabled,
            "rules": dict(policy.raw_rules),
            "updated_by": f"{actor.type}:{actor.id}",
            "updated_at": utcnow(),
        }
        exists = (
            await conn.execute(
                sa.select(model_policies.c.tenant_id).where(model_policies.c.tenant_id == tenant_id)
            )
        ).first()
        if exists is None:
            await conn.execute(sa.insert(model_policies).values(tenant_id=tenant_id, **values))
        else:
            await conn.execute(
                sa.update(model_policies)
                .where(model_policies.c.tenant_id == tenant_id)
                .values(**values)
            )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "model.policy_changed",
            {
                "enabled_before": before.enabled,
                "enabled": policy.enabled,
                "providers": list(policy.providers),
                "tasks": list(policy.tasks),
                "regions": list(policy.regions),
                "max_retention_days": policy.max_retention_days,
                "rules_sha256": audit.digest(dict(policy.raw_rules)),
            },
        )
        return policy


# ── The record of calls, from which budgets are counted ───────────────


@dataclass(frozen=True)
class Spent:
    tokens: int = 0
    cost_micros: int = 0
    calls: int = 0


def start_of_day(now: Optional[datetime] = None) -> datetime:
    """Midnight UTC. The daily budget is counted from here."""
    moment = (now or utcnow()).astimezone(timezone.utc)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


async def spent(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    run_id: Optional[uuid.UUID] = None,
    since: Optional[datetime] = None,
) -> Spent:
    """Tokens and cost recorded for a run, or for the tenant since a moment."""
    query = sa.select(
        sa.func.coalesce(sa.func.sum(model_calls.c.input_tokens + model_calls.c.output_tokens), 0),
        sa.func.coalesce(sa.func.sum(model_calls.c.cost_micros), 0),
        sa.func.count(),
    ).where(model_calls.c.tenant_id == tenant_id)
    if run_id is not None:
        query = query.where(model_calls.c.run_id == run_id)
    if since is not None:
        query = query.where(model_calls.c.created_at >= since)
    tokens, cost, calls = (await conn.execute(query)).one()
    return Spent(int(tokens), int(cost), int(calls))


async def stopped(conn: AsyncConnection, tenant_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    """True once a budget has ended the advisor's part in this run."""
    found = await conn.execute(
        sa.select(model_calls.c.id)
        .where(
            model_calls.c.tenant_id == tenant_id,
            model_calls.c.run_id == run_id,
            model_calls.c.outcome == "over_budget",
        )
        .limit(1)
    )
    return found.first() is not None


async def record_call(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    actor: Actor,
    *,
    run_id: Optional[uuid.UUID],
    subject: str,
    task: str,
    provider: str,
    model: str,
    prompt_version: str,
    prompt_digest: str,
    input_digest: str,
    output_digest: str,
    input_tokens: int,
    output_tokens: int,
    cost_micros: int,
    outcome: str,
    detail: str = "",
) -> None:
    """Record one call and its audit event together. No prompt or answer text is kept."""
    await conn.execute(
        sa.insert(model_calls).values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            run_id=run_id,
            subject=subject[:200],
            task=task,
            provider=provider,
            model=model[:200],
            prompt_version=prompt_version,
            input_digest=input_digest,
            output_digest=output_digest,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micros=cost_micros,
            outcome=outcome,
            detail=detail[:300],
            created_at=utcnow(),
        )
    )
    await audit.append(
        conn,
        tenant_id,
        actor,
        "model.budget_exceeded" if outcome == "over_budget" else "model.called",
        {
            "task": task,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "prompt_sha256": prompt_digest,
            "input_sha256": input_digest,
            "output_sha256": output_digest,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_micros": cost_micros,
            "outcome": outcome,
            "detail": detail[:300],
            "subject": subject[:200],
        },
        correlation_id=str(run_id) if run_id else "",
    )


async def usage(db: Database, tenant_id: uuid.UUID) -> Dict[str, Any]:
    """Today's use, for display."""
    since = start_of_day()
    async with db.tenant(tenant_id) as conn:
        total = await spent(conn, tenant_id, since=since)
        rows = (
            await conn.execute(
                sa.select(model_calls.c.outcome, sa.func.count())
                .where(model_calls.c.tenant_id == tenant_id, model_calls.c.created_at >= since)
                .group_by(model_calls.c.outcome)
            )
        ).all()
    return {
        "since": since.isoformat(),
        "calls": total.calls,
        "tokens": total.tokens,
        "cost_usd": round(total.cost_micros / MICROS, 6),
        "by_outcome": {outcome: int(count) for outcome, count in rows},
    }


async def recent_calls(
    db: Database, tenant_id: uuid.UUID, limit: int = 100
) -> List[Dict[str, Any]]:
    async with db.tenant(tenant_id) as conn:
        rows = (
            await conn.execute(
                sa.select(model_calls)
                .where(model_calls.c.tenant_id == tenant_id)
                .order_by(model_calls.c.created_at.desc())
                .limit(max(1, min(500, limit)))
            )
        ).all()
    return [
        {
            "id": str(r.id),
            "run_id": str(r.run_id) if r.run_id else "",
            "subject": r.subject,
            "task": r.task,
            "provider": r.provider,
            "model": r.model,
            "prompt_version": r.prompt_version,
            "input_sha256": r.input_digest,
            "output_sha256": r.output_digest,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cost_usd": round(r.cost_micros / MICROS, 6),
            "outcome": r.outcome,
            "detail": r.detail,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
