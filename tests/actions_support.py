"""A tenant with the restart SOP published, the mocks, and the action pipeline, for tests."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import sqlalchemy as sa

from src.l1_agent.actions import store as action_store
from src.l1_agent.actions.service import ActionService
from src.l1_agent.audit.log import Actor, events_for
from src.l1_agent.config.settings import ActionSettings
from src.l1_agent.db.models import jobs, runs
from src.l1_agent.demo import MockServiceNowClient, _restart_incident, build_mock_adapters
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.service.runs import RunService, record_by_number
from src.l1_agent.sop import store as sop_store
from tests.conftest import Backend, make_service

RESTART_FILE = (
    Path(__file__).resolve().parents[1] / "data" / "sample_sops" / "autosys_job_restart.json"
)
RESTART_SOP = "SOP-AUTOSYS-002"
FORCE_START = "autosys.job.force_start"
T1_JOB = "NIGHTLY_SETTLEMENT_LOAD"
T2_JOB = "PAYROLL_GL_POST"
FAILS_AGAIN = "NIGHTLY_RECON_LOAD"

AUTHOR = Actor("user", "https://idp.test|author-1")
REVIEWER = Actor("user", "https://idp.test|reviewer-1")
APPROVER = Actor("user", "https://idp.test|approver-1")
ADMIN = Actor("user", "https://idp.test|admin-1")
INTAKE = Actor.system("test-intake")


def restart_content() -> dict:
    return json.loads(RESTART_FILE.read_text(encoding="utf-8"))


@dataclass
class World:
    backend: Backend
    tenant: uuid.UUID
    snow: MockServiceNowClient
    adapters: Dict[str, Any]
    actions: ActionService
    service: RunService
    sop: SOP
    sleeps: List[float] = field(default_factory=list)
    _count: int = 0

    @property
    def db(self):
        return self.backend.db

    @property
    def autosys(self):
        return self.adapters["autosys"]

    async def ingest(self, job: str = T1_JOB) -> Incident:
        self._count += 1
        number = f"INC77{self._count:05d}"
        incident = Incident.from_servicenow(
            _restart_incident(f"sys-{uuid.uuid4().hex[:12]}", number, job, "Restart it.")
        )
        result = await self.service.ingest(self.tenant, incident, INTAKE)
        assert result.accepted
        return incident

    async def work(self) -> bool:
        return await self.service.work_once([self.tenant])

    async def run(self, job: str = T1_JOB) -> Incident:
        """One incident for `job`, worked until it ends or waits."""
        incident = await self.ingest(job)
        await self.work()
        return incident

    async def record(self, incident: Incident) -> Optional[dict]:
        return await record_by_number(self.db, self.tenant, incident.number)

    async def action(self, incident: Incident) -> Optional[dict]:
        found = [
            a
            for a in await action_store.list_actions(self.db, self.tenant)
            if a["incident_number"] == incident.number
        ]
        assert len(found) <= 1
        return found[0] if found else None

    async def run_state(self, incident: Incident) -> tuple:
        """(run state, outcome, job status) for the incident's run."""
        async with self.db.tenant(self.tenant) as conn:
            row = (
                await conn.execute(
                    sa.select(runs.c.state, runs.c.outcome, jobs.c.status)
                    .join(jobs, jobs.c.run_id == runs.c.id)
                    .where(runs.c.tenant_id == self.tenant, runs.c.number == incident.number)
                )
            ).one()
        return tuple(row)

    async def decide(self, incident: Incident, approve: bool = True, actor: Actor = APPROVER, **kw):
        action = await self.action(incident)
        return await action_store.decide(
            self.db,
            self.tenant,
            actor,
            uuid.UUID(action["id"]),
            approve=approve,
            seen_hash=kw.pop("seen_hash", action["action_hash"]),
            **kw,
        )

    async def events(self, incident: Incident) -> List[dict]:
        """The run's audit events, oldest first: everything recorded with its run id."""
        action = await self.action(incident)
        record = await self.record(incident)
        run_id = (action or record)["run_id"]
        return await events_for(self.db, self.tenant, correlation_id=run_id, limit=1000)

    async def set_policy(self, autonomy: Optional[str] = None, **rules: Any) -> None:
        current = dict((await action_store.get_policy(self.db, self.tenant)).raw_rules)
        current.update(rules)
        await action_store.set_policy(self.db, self.tenant, ADMIN, autonomy=autonomy, rules=current)


async def make_world(
    backend: Backend,
    *,
    autonomy: str = "automatic",
    sop_level: str = "automatic",
    rules: Optional[Dict[str, Any]] = None,
    settings: Optional[ActionSettings] = None,
    connector_settings: Optional[Dict[str, dict]] = None,
    author: Actor = AUTHOR,
) -> World:
    """A tenant whose restart SOP was written by `author` and approved by someone else.

    By default nightly jobs are on the automatic list and payroll jobs are raised to T2.
    """
    tenant = await backend.new_tenant("act")
    db = backend.db
    version_id = await sop_store.create_draft(db, tenant, author, restart_content())
    digest = await sop_store.submit(db, tenant, author, version_id)
    await sop_store.approve(db, tenant, REVIEWER, version_id, digest)
    await sop_store.publish(db, tenant, REVIEWER, version_id)
    sop = next(s for s in await sop_store.published(db, tenant) if s.sop_id == RESTART_SOP)

    policy_rules: Dict[str, Any] = {
        "sop_autonomy": {RESTART_SOP: {"level": sop_level, "content_hash": sop.content_hash}},
        "automatic": [{"sop_id": RESTART_SOP, "capability": FORCE_START, "targets": ["NIGHTLY_*"]}],
        "raise_tier": [{"capability": FORCE_START, "targets": ["PAYROLL_*"]}],
    }
    policy_rules.update(rules or {})
    await action_store.set_policy(db, tenant, ADMIN, autonomy=autonomy, rules=policy_rules)

    snow = MockServiceNowClient()
    adapters = build_mock_adapters()
    sleeps: List[float] = []

    async def no_wait(seconds: float) -> None:
        sleeps.append(seconds)

    actions = ActionService(
        db, snow, settings or ActionSettings(servicenow_poll_seconds=0), sleep=no_wait
    )
    service = make_service(
        db,
        adapters=adapters,
        snow=snow,
        sops_from_store=True,
        actions=actions,
        connector_settings=connector_settings,
    )
    return World(backend, tenant, snow, adapters, actions, service, sop, sleeps)


def reconstruct(events: List[dict]) -> Dict[str, Any]:
    """What happened in a run, from its audit events and nothing else."""
    story: Dict[str, Any] = {"steps": [], "suspended": 0, "action": {}, "types": []}
    action = story["action"]
    for event in events:
        kind, payload = event["event_type"], event["payload"]
        story["types"].append(kind)
        if kind == "incident.received":
            story["incident"] = payload["number"]
        elif kind == "run.sop_matched":
            story["sop"] = (payload["sop_id"], payload["sop_version"], payload["sop_sha256"])
        elif kind == "step.completed":
            story["steps"].append((payload["step_id"], payload["status"]))
        elif kind == "policy.decided":
            action.update(
                {
                    k: payload[k]
                    for k in ("capability", "parameters", "tier", "action_sha256")
                    if k in payload
                }
            )
            action["verdict"] = payload["verdict"]
            action["reason_code"] = payload["reason_code"]
        elif kind == "change.opened":
            action["change"] = payload["change"]
        elif kind == "approval.requested":
            action["asked"] = payload["channels"]
        elif kind == "approval.decided":
            action["decision"] = payload["decision"]
            action["decided_by"] = f"{event['actor']['type']}:{event['actor']['id']}"
            action["decided_on"] = payload["action_sha256"]
        elif kind == "action.executing":
            action["executed"] = payload["parameters"]
            action["authorised_by"] = payload["authorised_by"]
        elif kind in ("action.verified", "action.failed", "action.rolled_back"):
            action["result"] = kind.split(".")[1]
        elif kind == "change.closed":
            action["change_closed_successful"] = payload["successful"]
        elif kind == "run.suspended":
            story["suspended"] += 1
        elif kind == "run.concluded":
            story["outcome"] = payload["outcome"]
            story["verified_by"] = payload["verified_by"]
    return story
