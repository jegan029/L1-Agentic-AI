"""Demo mode: run sample incidents through the real processor with mock adapters.

Shows each terminal outcome the engine can reach from the sample SOPs:
a verified resolution, escalations with evidence, and a no-SOP escalation.
Then three incidents whose SOP restarts a failed job: one done automatically,
one that waits for an approval, and one whose restart does not work.
Nothing here talks to a real system.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import List

from src.l1_agent.adapters.mock_adapters import (
    MockAutosysAdapter,
    MockDynatraceAdapter,
    MockIR360Adapter,
    MockMainframeAdapter,
    MockSplunkAdapter,
    MockWebUIScraperAdapter,
    MockWindowsShareAdapter,
)
from src.l1_agent.ai.analyzer import AIAnalyzer
from src.l1_agent.ai.mock_llm import MockLLMClient
from src.l1_agent.connectors.itsm.base import (
    ChangeApproval,
    ChangeRef,
    ChangeUnavailable,
    ClaimResult,
)
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.sop.validator import ensure_valid
from src.l1_agent.utils.logging import get_logger, setup_logging

logger = get_logger("demo")

SAMPLE_SOP_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "sample_sops"
SAMPLE_DRAFT_DIR = SAMPLE_SOP_DIR.parent / "sample_drafts"

# ── Sample data ───────────────────────────────────────────────────────

SAMPLE_INCIDENT = {
    "sys_id": "demo-inc-001",
    "number": "INC0012345",
    "short_description": "MQ queue depth high on PAYMENT.REQUEST - messages not being consumed",
    "description": (
        "The PAYMENT.REQUEST queue on queue manager QMPROD01 has been "
        "building up since 09:00. Consumer application PaymentService "
        "appears to be not processing messages. Priority: High."
    ),
    "category": "Middleware",
    "subcategory": "MQ",
    "cmdb_ci": "PaymentService",
    "assignment_group": "L1-Middleware-Support",
    "priority": "2",
    "state": "1",
    "caller_id": "auto-monitor",
    "sys_created_on": "2026-04-14T09:10:00Z",
}

DEMO_INCIDENTS: List[dict] = [
    SAMPLE_INCIDENT,
    {
        "sys_id": "demo-mq-002",
        "number": "INC0009005",
        "short_description": "MQ queue depth high on ORDER.REQUEST",
        "description": (
            "Queue depth alert for ORDER.REQUEST on queue manager QMPROD01. "
            "Consumers were restarted by the application team at 09:20."
        ),
        "category": "Middleware",
        "subcategory": "MQ",
        "cmdb_ci": "OrderService",
        "assignment_group": "L1-Middleware-Support",
        "priority": "3",
        "state": "1",
    },
    {
        "sys_id": "demo-autosys-001",
        "number": "INC0009003",
        "short_description": (
            "Autosys batch job failure - ETL pipeline job BATCH_PAYMENT_PROCESS status FA"
        ),
        "description": (
            "The Autosys job BATCH_PAYMENT_PROCESS has entered FA state. "
            "Job ran at 02:00 but failed with exit code 1."
        ),
        "category": "Batch",
        "subcategory": "Scheduling",
        "cmdb_ci": "ETL-Pipeline",
        "assignment_group": "L1-Batch-Support",
        "priority": "3",
        "state": "1",
    },
    {
        "sys_id": "demo-mem-001",
        "number": "INC0009004",
        "short_description": "High memory usage alert on app-server-prod01 - memory at 92%",
        "description": (
            "Dynatrace has triggered a memory saturation alert for app-server-prod01. "
            "Memory usage is at 92% of total 32GB. OOM risk detected."
        ),
        "category": "Infrastructure",
        "subcategory": "Server",
        "cmdb_ci": "AppServerProd01",
        "assignment_group": "L1-Infra-Support",
        "priority": "2",
        "state": "1",
    },
    {
        "sys_id": "demo-esc-001",
        "number": "INC0009002",
        "short_description": "SSL certificate expiring in 3 days on payments-api.internal",
        "description": (
            "SSL certificate for payments-api.internal expires in 72 hours. "
            "Automated renewal failed."
        ),
        "category": "Security",
        "subcategory": "Certificates",
        "cmdb_ci": "payments-api.internal",
        "assignment_group": "L1-Security-Support",
        "priority": "1",
        "state": "1",
    },
]


# Incidents for the restart SOP (SOP-AUTOSYS-002). See `seed_demo_policy` for why each
# takes the route it does.
def _restart_incident(sys_id: str, number: str, job: str, why: str) -> dict:
    return {
        "sys_id": sys_id,
        "number": number,
        "short_description": f"Nightly settlement job {job} failed - restart required",
        "description": f"The nightly settlement run stopped at job {job}. {why}",
        "category": "Batch",
        "subcategory": "Scheduling",
        "cmdb_ci": "Settlement",
        "assignment_group": "L1-Batch-Support",
        "priority": "3",
        "state": "1",
    }


ACTION_DEMO_INCIDENTS: List[dict] = [
    _restart_incident(
        "demo-restart-001", "INC0009006", "NIGHTLY_SETTLEMENT_LOAD", "A rerun is usually enough."
    ),
    _restart_incident(
        "demo-restart-002", "INC0009007", "PAYROLL_GL_POST", "This job posts to the ledger."
    ),
    _restart_incident(
        "demo-restart-003", "INC0009008", "NIGHTLY_RECON_LOAD", "It failed on its input file."
    ),
]
DEMO_RESTART_SOP = "SOP-AUTOSYS-002"


def sample_sop_contents() -> List[dict]:
    """The sample SOP files as they are on disk."""
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SAMPLE_SOP_DIR.glob("*.json"))
    ]


def sample_draft_contents() -> List[dict]:
    """Sample SOPs that are waiting for review, so the review screen has something in it."""
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SAMPLE_DRAFT_DIR.glob("*.json"))
    ]


def load_sample_sops() -> List[SOP]:
    """Load and validate every sample SOP. An invalid sample is a bug, so raise."""
    sops: List[SOP] = []
    for content in sample_sop_contents():
        sop = SOP.from_dict(content)
        ensure_valid(sop)
        sops.append(sop)
    return sops


async def seed_demo_sops(db, tenant_id) -> None:
    """Publish the sample SOPs, and leave the sample drafts in review, for a demo tenant.

    They go through the same lifecycle as any SOP, written and approved by two
    named system actors, so the audit log shows where they came from.
    """
    from src.l1_agent.sop import store

    await store.seed_published(db, tenant_id, sample_sop_contents())
    known = {v["sop_id"] for v in await store.list_versions(db, tenant_id)}
    for content in sample_draft_contents():
        if content.get("sop_id") in known:
            continue
        version_id = await store.create_draft(
            db, tenant_id, store.SAMPLE_AUTHOR, content, origin=store.ORIGIN_SEED
        )
        await store.submit(db, tenant_id, store.SAMPLE_AUTHOR, version_id)


async def seed_demo_policy(db, tenant_id) -> None:
    """Let the demo tenant act, so the demo shows more than shadow mode.

    Nightly jobs are restarted automatically. Payroll jobs are raised to T2, so
    they wait for an approval. A tenant that already has a policy is left alone.
    Nothing here applies outside demo mode, where a new tenant starts in shadow.
    """
    from src.l1_agent.actions import store as actions
    from src.l1_agent.audit.log import Actor
    from src.l1_agent.sop import store

    if (await actions.get_policy(db, tenant_id)).updated_at is not None:
        return
    restart = next(
        (s for s in await store.published(db, tenant_id) if s.sop_id == DEMO_RESTART_SOP), None
    )
    if restart is None:
        return
    capability = "autosys.job.force_start"
    await actions.set_policy(
        db,
        tenant_id,
        Actor.system("demo-setup"),
        autonomy="automatic",
        rules={
            "sop_autonomy": {
                DEMO_RESTART_SOP: {"level": "automatic", "content_hash": restart.content_hash}
            },
            "automatic": [
                {"sop_id": DEMO_RESTART_SOP, "capability": capability, "targets": ["NIGHTLY_*"]}
            ],
            "raise_tier": [{"capability": capability, "targets": ["PAYROLL_*"]}],
        },
    )


async def seed_demo_model_policy(db, tenant_id, provider: str = "") -> None:
    """Let the demo tenant use the deterministic stand-in for a model, for every task.

    `provider` names a hosted provider to allow instead. It is given only when
    the operator asked for it with LIVE_ADAPTERS=model: a key in the environment
    is not enough for ticket text to leave for a hosted model.

    A tenant that already has a model policy is left alone. Outside demo mode a
    tenant uses no model until its administrator sets a policy (ADR 0009).
    """
    import sqlalchemy as sa

    from src.l1_agent.ai import policy as model_policy
    from src.l1_agent.ai.prompts import TASKS
    from src.l1_agent.ai.providers import DETERMINISTIC
    from src.l1_agent.audit.log import Actor
    from src.l1_agent.db.models import model_policies

    async with db.tenant(tenant_id) as conn:
        exists = (
            await conn.execute(
                sa.select(model_policies.c.tenant_id).where(model_policies.c.tenant_id == tenant_id)
            )
        ).first()
    if exists is not None:
        return
    await model_policy.set_model_policy(
        db,
        tenant_id,
        Actor.system("demo-setup"),
        enabled=True,
        rules={"providers": [provider or DETERMINISTIC], "tasks": list(TASKS)},
    )


def build_mock_adapters() -> dict:
    return {
        "splunk": MockSplunkAdapter(),
        "ir360": MockIR360Adapter(),
        "windows_share": MockWindowsShareAdapter(),
        "autosys": MockAutosysAdapter(),
        "dynatrace": MockDynatraceAdapter(),
        "webui": MockWebUIScraperAdapter(),
        "mainframe": MockMainframeAdapter(),
    }


class MockServiceNowClient:
    """In-memory mock for demo mode that captures work notes."""

    def __init__(self) -> None:
        self.work_notes: list = []
        self.updates: list = []
        # Change records, keyed by number. `change_templates` None accepts any template name.
        self.changes: dict = {}
        self.change_templates = None
        self.approval_groups: dict = {}  # group name -> number of members

    async def add_work_note(self, sys_id: str, note: str) -> dict:
        self.work_notes.append({"sys_id": sys_id, "note": note})
        return {"result": {"sys_id": sys_id}}

    async def update_incident(self, sys_id: str, fields: dict) -> dict:
        self.updates.append({"sys_id": sys_id, "fields": fields})
        return {"result": {"sys_id": sys_id}}

    async def resolve_incident(self, sys_id: str, close_notes: str) -> dict:
        return await self.update_incident(sys_id, {"state": "6", "close_notes": close_notes})

    async def claim(self, incident: Incident) -> ClaimResult:
        return ClaimResult(True)

    # ── Change records (ADR 0008) ─────────────────────────────────────

    async def open_change(
        self,
        template: str,
        *,
        correlation: str,
        short_description: str,
        description: str,
        incident_sys_id: str,
    ) -> ChangeRef:
        for number, change in self.changes.items():
            if change["correlation"] == correlation:
                return ChangeRef(change["sys_id"], number)
        if self.change_templates is not None and template not in self.change_templates:
            raise ChangeUnavailable(f"there is no active standard change template '{template}'")
        number = f"CHG{len(self.changes) + 30001:07d}"
        self.changes[number] = {
            "sys_id": f"chg-{len(self.changes) + 1}",
            "template": template,
            "correlation": correlation,
            "short_description": short_description,
            "description": description,
            "incident": incident_sys_id,
            "notes": [],
            "asked": "",
            "approval": ChangeApproval("requested"),
            "closed": None,
        }
        return ChangeRef(self.changes[number]["sys_id"], number)

    async def request_change_approval(self, change: ChangeRef, group: str) -> int:
        members = self.approval_groups.get(group, 1)
        if not members:
            raise ChangeUnavailable(f"approval group '{group}' has no active members")
        self.changes[change.number]["asked"] = group
        return members

    async def change_approval(self, change: ChangeRef) -> ChangeApproval:
        record = self.changes[change.number]
        found: ChangeApproval = record["approval"]
        return ChangeApproval(
            found.state, found.approver, found.approver_email, found.comment, record["correlation"]
        )

    def decide_change(
        self, number: str, state: str, approver: str, email: str = "", comment: str = ""
    ) -> None:
        """What an approver does in ServiceNow: approve or reject the change."""
        self.changes[number]["approval"] = ChangeApproval(state, approver, email, comment)

    async def note_change(self, change: ChangeRef, note: str) -> None:
        self.changes[change.number]["notes"].append(note)

    async def close_change(self, change: ChangeRef, successful: bool, notes: str) -> None:
        self.changes[change.number]["closed"] = {"successful": successful, "notes": notes}

    async def get_sops(self, query: str = "", limit: int = 50) -> list:
        return []

    async def get_new_incidents(self, **kwargs) -> list:
        return []

    async def close(self) -> None:
        pass


def build_demo_processor(
    snow: MockServiceNowClient, event_bus=None, actions=None, advisors=None
) -> IncidentProcessor:
    """The same processor the service uses, wired to mocks.

    It is built with no SOPs of its own: the run service hands it the
    tenant's published versions for each run. `advisors` is the model gateway;
    without one the older mock model client ranks SOPs, with no record kept.
    """
    llm = None if advisors is not None else MockLLMClient()
    return IncidentProcessor(
        snow_client=snow,  # type: ignore[arg-type]
        sop_matcher=SOPMatcher(confidence_threshold=0.6),
        sop_parser=SOPParser(),
        executor=SOPExecutor(
            adapters=build_mock_adapters(),
            retry_max_attempts=1,
            retry_base_delay=0,
            event_bus=event_bus,
            actions=actions,
        ),
        confidence_threshold=0.6,
        llm_client=llm,
        ai_analyzer=AIAnalyzer(llm_client=llm) if llm else None,
        event_bus=event_bus,
        l2_router=L2Router(),
        advisors=advisors,
    )


async def run_demo() -> List[dict]:
    """Run the demo scenarios end-to-end. Returns one result row per incident.

    Uses the same path as the service: intake, the job queue, the worker and
    the audit log, on a throwaway SQLite database.
    """
    # Imported here: the service layer imports this module for its mocks.
    from src.l1_agent.actions import store as action_store
    from src.l1_agent.actions.service import ActionService
    from src.l1_agent.ai import policy as model_policy
    from src.l1_agent.ai.gateway import ModelGateway
    from src.l1_agent.ai.providers import DeterministicProvider
    from src.l1_agent.audit.log import Actor, verify
    from src.l1_agent.db.engine import DEMO_TENANT_SLUG, Database
    from src.l1_agent.service.runs import DbEventBus, RunService, record_by_number

    setup_logging("WARNING")
    print("\n" + "=" * 72)
    print("  L1 ENGINEER AGENT - DEMO (mock adapters, mock ServiceNow, mock model)")
    print("=" * 72)

    snow = MockServiceNowClient()
    db = Database.create("", demo_mode=True)
    rows: List[dict] = []
    try:
        await db.create_schema_for_demo()
        tenant_id = await db.ensure_tenant(DEMO_TENANT_SLUG, "Demo tenant")
        await seed_demo_sops(db, tenant_id)
        await seed_demo_policy(db, tenant_id)
        await seed_demo_model_policy(db, tenant_id)
        actions = ActionService(db, snow)
        model = ModelGateway(db, DeterministicProvider())
        service = RunService(
            db,
            build_demo_processor(snow, DbEventBus(db), actions, model),
            sops_from_store=True,
            actions=actions,
        )
        intake = Actor.system("demo")
        for payload in DEMO_INCIDENTS + ACTION_DEMO_INCIDENTS:
            incident = Incident.from_servicenow(payload)
            await service.ingest(tenant_id, incident, intake)
            await service.work_once([tenant_id])
            record = await record_by_number(db, tenant_id, incident.number)
            waited = record is None
            if waited:
                # The run is parked, waiting for a person. Here the person is the demo.
                asked = (await action_store.list_actions(db, tenant_id, status="pending"))[0]
                print(f"\n--- {incident.number}: {incident.short_description}")
                print(
                    f"  Waiting for approval: {asked['capability']} {asked['parameters']} "
                    f"(tier {asked['tier']}, change {asked['change_number']})"
                )
                await action_store.decide(
                    db,
                    tenant_id,
                    Actor("user", "demo|approver"),
                    uuid.UUID(asked["id"]),
                    approve=True,
                    seen_hash=asked["action_hash"],
                )
                print("  Approved by demo|approver; the run resumes")
                await service.work_once([tenant_id])
                record = await record_by_number(db, tenant_id, incident.number)
            assert record is not None
            rows.append(
                {
                    "incident": incident.number,
                    "sop": record["sop_id"] or "(none)",
                    "outcome": record["outcome"],
                    "verified_by": record["verified_by"],
                    "reason": record["escalation_reason"],
                }
            )
            if not waited:
                print(f"\n--- {incident.number}: {incident.short_description}")
            print(f"  SOP:      {record['sop_id'] or '(no SOP matched)'}")
            print(f"  Outcome:  {record['outcome'].upper()}")
            if record["verified_by"]:
                print(f"  Verified: step {record['verified_by']} passed")
            if record["escalation_reason"]:
                print(f"  Reason:   {record['escalation_reason']}")
            for r in record["step_results"]:
                first_line = r["output_summary"].splitlines()[0] if r["output_summary"] else ""
                print(f"    [{r['status']:9}] {r['step_id']} ({r['step_type']}) {first_line[:70]}")

        # A second delivery of the first incident is recognised and ignored.
        again = await service.ingest(tenant_id, Incident.from_servicenow(SAMPLE_INCIDENT), intake)
        status = "accepted" if again.accepted else "duplicate"
        print(f"\n--- Redelivery of {SAMPLE_INCIDENT['number']}: {status} (no run created)")

        chain = await verify(db, tenant_id)
        print(f"\nWork notes posted to the mock ServiceNow: {len(snow.work_notes)}")
        print(f"Change records opened in the mock ServiceNow: {len(snow.changes)}")
        used = await model_policy.usage(db, tenant_id)
        print(
            f"Model requests (deterministic stand-in, each one audited): {used['calls']} "
            f"({used['tokens']} tokens)"
        )
        print(
            f"Audit log: {chain.events_checked} events, "
            f"hash chain {'verified' if chain.ok else 'BROKEN'}"
        )
    finally:
        await db.close()
    print("=" * 72 + "\n")
    return rows


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
