"""A scripted model provider and helpers for the model gateway tests."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable, Dict, List, Optional

import sqlalchemy as sa

from src.l1_agent.ai import policy as model_policy
from src.l1_agent.ai.gateway import ModelGateway, Scope
from src.l1_agent.ai.prompts import PROMPTS, TASKS
from src.l1_agent.ai.providers import OPENAI_COMPATIBLE, Completion, ToolSpec
from src.l1_agent.ai.redaction import Redactor
from src.l1_agent.audit.log import Actor, events_for
from src.l1_agent.db.models import model_calls, runs
from src.l1_agent.demo import DEMO_INCIDENTS
from src.l1_agent.models.incident import Incident
from tests.conftest import Backend, make_service

ADMIN = Actor("user", "https://idp.test|admin-1")
INTAKE = Actor.system("test-intake")
MARKER = "the-marker-text-of-this-prompt"
ANSWER = {"sop_id": "SOP-1", "rationale": "the-marker-text-of-this-answer"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sop_id": {"type": "string", "enum": ["SOP-1", "NONE"]},
        "rationale": {"type": "string", "maxLength": 600},
    },
    "required": ["sop_id", "rationale"],
}


class Scripted:
    """A provider that answers from a script. `answers` maps a tool name to an answer,
    to a callable taking (user, tool), or to an exception to raise."""

    def __init__(
        self,
        answers: Optional[Dict[str, Any]] = None,
        *,
        kind: str = OPENAI_COMPATIBLE,
        region: str = "eu",
        retention_days: Optional[int] = 0,
        usage: Optional[tuple] = (100, 20),
        prices: tuple = (0.0, 0.0),
        delay: float = 0.0,
    ) -> None:
        self.id = kind
        self.model = "scripted-1"
        self.region = region
        self.retention_days = retention_days
        self.input_price_per_mtok, self.output_price_per_mtok = prices
        self.answers = answers if answers is not None else {"select_sop": ANSWER}
        self.usage = usage
        self.delay = delay
        self.requests: List[tuple] = []

    async def complete(
        self, system: str, user: str, tool: ToolSpec, max_output_tokens: int
    ) -> Completion:
        self.requests.append((system, user, tool))
        if self.delay:
            await asyncio.sleep(self.delay)
        answer = self.answers.get(tool.name)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = answer(user, tool)
        counted = self.usage or (None, None)
        return Completion(answer, counted[0], counted[1])

    async def close(self) -> None:
        return None


async def allow(db, tenant_id, provider: str = OPENAI_COMPATIBLE, **rules: Any):
    """Switch the model on for a tenant, for every task unless `tasks` says otherwise."""
    rules.setdefault("providers", [provider])
    rules.setdefault("tasks", list(TASKS))
    return await model_policy.set_model_policy(db, tenant_id, ADMIN, enabled=True, rules=rules)


async def new_run(backend: Backend, tenant_id: uuid.UUID, index: int = 0) -> uuid.UUID:
    """A run record to count model calls against. It is not worked."""
    incident = Incident.from_servicenow(
        {
            **DEMO_INCIDENTS[index],
            "sys_id": uuid.uuid4().hex,
            "number": f"INC{uuid.uuid4().int % 10**7:07d}",
        }
    )
    await make_service(backend.db).ingest(tenant_id, incident, INTAKE)
    async with backend.db.tenant(tenant_id) as conn:
        return (
            await conn.execute(
                sa.select(runs.c.id).where(
                    runs.c.tenant_id == tenant_id, runs.c.number == incident.number
                )
            )
        ).scalar_one()


async def ask(
    gateway: ModelGateway,
    tenant_id: uuid.UUID,
    run_id: Optional[uuid.UUID] = None,
    *,
    task: str = "rank",
    text: str = MARKER,
    max_output_tokens: int = 100,
    redactor: Optional[Redactor] = None,
    schema: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    return await gateway.ask(
        Scope(tenant_id, run_id, "INC-TEST"),
        redactor or Redactor(),
        PROMPTS[task],
        trusted="Candidates: SOP-1",
        sections=[("Description", text)],
        parameters=schema or SCHEMA,
        max_output_tokens=max_output_tokens,
    )


async def calls(db, tenant_id: uuid.UUID) -> List[Any]:
    async with db.tenant(tenant_id) as conn:
        return list(
            (
                await conn.execute(
                    sa.select(model_calls)
                    .where(model_calls.c.tenant_id == tenant_id)
                    .order_by(model_calls.c.created_at)
                )
            ).all()
        )


async def audit_types(db, tenant_id: uuid.UUID, kind: Callable[[str], bool] = lambda t: True):
    return [e for e in await events_for(db, tenant_id, limit=1000) if kind(e["event_type"])]
