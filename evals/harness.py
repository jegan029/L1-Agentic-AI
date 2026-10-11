"""A demo tenant on a throwaway database, with every capability call recorded.

The lab is built the way the service builds itself: intake, the job queue, a
worker, the published sample SOPs, the mock connectors, the action pipeline and
the model gateway. The one addition is a recorder inside each connector's
`invoke`, which is where a capability call becomes a call on a target system.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import sqlalchemy as sa

from src.l1_agent.actions import store as action_store
from src.l1_agent.actions.service import ActionService
from src.l1_agent.ai import policy as model_policy
from src.l1_agent.ai.gateway import ModelGateway
from src.l1_agent.ai.prompts import TASKS
from src.l1_agent.audit.log import Actor
from src.l1_agent.connectors.catalog import connectors_for_adapters
from src.l1_agent.connectors.sdk.manifest import Kind
from src.l1_agent.db.engine import DEMO_TENANT_SLUG, Database
from src.l1_agent.db.models import model_calls, runs
from src.l1_agent.demo import (
    MockServiceNowClient,
    build_mock_adapters,
    seed_demo_policy,
    seed_demo_sops,
)
from src.l1_agent.engine.executor import SOPExecutor, _FixedConnectors
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.service.runs import DbEventBus, RunService, record_by_number
from src.l1_agent.sop import store as sop_store

HERE = Path(__file__).resolve().parent
SAMPLE_MQ_SOP = HERE.parent / "data" / "sample_sops" / "mq_queue_depth_high.json"
ASSISTED_SOP = "SOP-EVAL-ASSIST"
THRESHOLD = 0.6
INTAKE = Actor.system("evals")
ADMIN = Actor.system("evals-admin")
APPROVER = Actor("user", "evals|approver")


def corpus(name: str) -> Any:
    return json.loads((HERE / f"{name}.json").read_text(encoding="utf-8"))


def assisted_sop() -> Dict[str, Any]:
    """The sample MQ procedure, with a queue manager that a model may help to locate.

    The author's pattern only finds the name after the words "queue manager".
    A ticket that puts it another way leaves the parameter unbound, and the SOP
    allows a model to point at it.
    """
    content = json.loads(SAMPLE_MQ_SOP.read_text(encoding="utf-8"))
    content["sop_id"] = ASSISTED_SOP
    content["title"] = "Broker backlog - investigation (model-assisted parameter)"
    content["keywords"] = ["broker backlog", "backlog", "broker"]
    for key in ("applicable_services", "applicable_categories", "applicable_assignment_groups"):
        content[key] = []
    for spec in content["parameters"]:
        if spec["name"] == "queue_manager":
            spec["source"] = {
                "kind": "extraction",
                "fields": ["short_description", "description"],
                "regex": r"queue manager (QM[A-Z0-9_]{1,45})\b",
                "advisor": True,
            }
    return content


@dataclass(frozen=True)
class Call:
    """One capability call, as a connector received it."""

    capability: str
    kind: str  # read | write
    parameters: Dict[str, Any]
    step_id: str
    dry_run: bool = False


@dataclass
class Result:
    incident: Incident
    record: Dict[str, Any]
    calls: List[Call]
    notes: List[str]
    advised: Dict[str, str] = field(default_factory=dict)
    model_requests: int = 0

    @property
    def outcome(self) -> str:
        return self.record["outcome"]

    @property
    def sop_id(self) -> str:
        return self.record["sop_id"]

    @property
    def writes(self) -> List[Call]:
        return [c for c in self.calls if c.kind == "write" and not c.dry_run]


async def _no_sleep(_: float) -> None:
    return None


class Lab:
    def __init__(self) -> None:
        self.db: Database
        self.tenant: uuid.UUID
        self.snow = MockServiceNowClient()
        self.calls: List[Call] = []
        self.provider: Any = None
        self.gateway: Optional[ModelGateway] = None
        self.service: RunService
        self.registry: Any

    @classmethod
    async def create(
        cls,
        provider: Any = None,
        *,
        rules: Optional[Dict[str, Any]] = None,
        tool_output_suffix: str = "",
        timeout_seconds: float = 0.05,
    ) -> "Lab":
        """`provider` None is a deployment with no model at all."""
        lab = cls()
        lab.db = Database.create("", demo_mode=True)
        await lab.db.create_schema_for_demo()
        lab.tenant = await lab.db.ensure_tenant(DEMO_TENANT_SLUG, "Evaluation tenant")
        await seed_demo_sops(lab.db, lab.tenant)
        await sop_store.seed_published(lab.db, lab.tenant, [assisted_sop()])
        await seed_demo_policy(lab.db, lab.tenant)
        lab.provider = provider
        if provider is not None:
            await model_policy.set_model_policy(
                lab.db,
                lab.tenant,
                ADMIN,
                enabled=True,
                rules=rules or {"providers": [provider.id], "tasks": list(TASKS)},
            )
            lab.gateway = ModelGateway(lab.db, provider, timeout_seconds=timeout_seconds)

        adapters = build_mock_adapters()
        if tool_output_suffix:
            for adapter in adapters.values():
                _append_to_output(adapter, tool_output_suffix)
        connectors = connectors_for_adapters(adapters)
        lab.registry = connectors.registry
        for connector in connectors.connectors().values():
            lab._record(connector)

        actions = ActionService(lab.db, lab.snow, sleep=_no_sleep)
        bus = DbEventBus(lab.db)
        processor = IncidentProcessor(
            snow_client=lab.snow,  # type: ignore[arg-type]
            sop_matcher=SOPMatcher(confidence_threshold=THRESHOLD),
            sop_parser=None,
            executor=SOPExecutor(
                connectors=_FixedConnectors(connectors),
                retry_max_attempts=1,
                retry_base_delay=0,
                event_bus=bus,
                actions=actions,
            ),
            confidence_threshold=THRESHOLD,
            event_bus=bus,
            l2_router=L2Router(),
            advisors=lab.gateway,
        )
        lab.service = RunService(lab.db, processor, sops_from_store=True, actions=actions)
        return lab

    def _record(self, connector: Any) -> None:
        inner = connector.invoke

        async def invoke(capability_id, parameters, context, grant=None, *, dry_run=False):
            capability = connector.manifest.capability(capability_id)
            self.calls.append(
                Call(
                    capability_id,
                    "write" if capability and capability.kind == Kind.WRITE else "read",
                    copy.deepcopy(dict(parameters)),
                    getattr(context, "step_id", ""),
                    dry_run,
                )
            )
            return await inner(capability_id, parameters, context, grant, dry_run=dry_run)

        connector.invoke = invoke

    async def published(self) -> Dict[str, SOP]:
        return {sop.sop_id: sop for sop in await sop_store.published(self.db, self.tenant)}

    async def run(self, payload: Dict[str, Any], *, approve: bool = True) -> Result:
        """Take one incident from intake to its outcome. A pending approval is given."""
        incident = Incident.from_servicenow(payload)
        first_call, first_note = len(self.calls), len(self.snow.work_notes)
        accepted = await self.service.ingest(self.tenant, incident, INTAKE)
        assert accepted.accepted, f"{incident.number} was not accepted"
        await self.service.work_once([self.tenant])
        record = await record_by_number(self.db, self.tenant, incident.number)
        if record is None and approve:
            for asked in await action_store.list_actions(self.db, self.tenant, status="pending"):
                await action_store.decide(
                    self.db,
                    self.tenant,
                    APPROVER,
                    uuid.UUID(asked["id"]),
                    approve=True,
                    seen_hash=asked["action_hash"],
                )
            await self.service.work_once([self.tenant])
            record = await record_by_number(self.db, self.tenant, incident.number)
        assert record is not None, f"{incident.number} has no outcome"
        async with self.db.tenant(self.tenant) as conn:
            run = (
                await conn.execute(
                    sa.select(runs.c.id, runs.c.advised_parameters).where(
                        runs.c.tenant_id == self.tenant, runs.c.number == incident.number
                    )
                )
            ).one()
            requests = (
                await conn.execute(
                    sa.select(sa.func.count()).where(
                        model_calls.c.tenant_id == self.tenant, model_calls.c.run_id == run.id
                    )
                )
            ).scalar()
        return Result(
            incident,
            record,
            self.calls[first_call:],
            [n["note"] for n in self.snow.work_notes[first_note:]],
            dict(run.advised_parameters or {}),
            int(requests or 0),
        )

    async def close(self) -> None:
        await self.db.close()


def _append_to_output(adapter: Any, suffix: str) -> None:
    """Make a mock tool return hostile text in what it reports."""
    inner = adapter.execute

    async def execute(parameters):
        result = await inner(parameters)
        result.evidence_snippet = f"{result.evidence_snippet or ''} {suffix}".strip()
        result.raw_output = f"{result.raw_output or ''}\n{suffix}"
        return result

    adapter.execute = execute
