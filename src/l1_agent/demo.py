"""Demo mode: run sample incidents through the real processor with mock adapters.

Shows each terminal outcome the engine can reach from the sample SOPs:
a verified resolution, escalations with evidence, and a no-SOP escalation.
Nothing here talks to a real system.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
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
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.sop.validator import ensure_valid
from src.l1_agent.store.incident_history import IncidentHistoryStore
from src.l1_agent.utils.logging import get_logger, setup_logging

logger = get_logger("demo")

SAMPLE_SOP_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "sample_sops"

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


def load_sample_sops() -> List[SOP]:
    """Load and validate every sample SOP. An invalid sample is a bug, so raise."""
    sops: List[SOP] = []
    for path in sorted(SAMPLE_SOP_DIR.glob("*.json")):
        sop = SOP.from_dict(json.loads(path.read_text(encoding="utf-8")))
        ensure_valid(sop)
        sops.append(sop)
    return sops


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

    async def add_work_note(self, sys_id: str, note: str) -> dict:
        self.work_notes.append({"sys_id": sys_id, "note": note})
        return {"result": {"sys_id": sys_id}}

    async def update_incident(self, sys_id: str, fields: dict) -> dict:
        self.updates.append({"sys_id": sys_id, "fields": fields})
        return {"result": {"sys_id": sys_id}}

    async def get_sops(self, query: str = "", limit: int = 50) -> list:
        return []

    async def get_new_incidents(self, **kwargs) -> list:
        return []

    async def close(self) -> None:
        pass


def build_demo_processor(snow: MockServiceNowClient, history_path: Path) -> IncidentProcessor:
    """The same processor the service uses, wired to mocks."""
    llm = MockLLMClient()
    return IncidentProcessor(
        snow_client=snow,  # type: ignore[arg-type]
        sop_matcher=SOPMatcher(confidence_threshold=0.6),
        sop_parser=SOPParser(),
        executor=SOPExecutor(
            adapters=build_mock_adapters(), retry_max_attempts=1, retry_base_delay=0
        ),
        sop_cache=load_sample_sops(),
        confidence_threshold=0.6,
        llm_client=llm,
        ai_analyzer=AIAnalyzer(llm_client=llm),
        history_store=IncidentHistoryStore(path=history_path),
        l2_router=L2Router(),
    )


async def run_demo() -> List[dict]:
    """Run the demo scenarios end-to-end. Returns one result row per incident."""
    setup_logging("WARNING")
    print("\n" + "=" * 72)
    print("  L1 ENGINEER AGENT - DEMO (mock adapters, mock ServiceNow, mock model)")
    print("=" * 72)

    snow = MockServiceNowClient()
    rows: List[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        processor = build_demo_processor(snow, Path(tmp) / "history.jsonl")
        for payload in DEMO_INCIDENTS:
            incident = Incident.from_servicenow(payload)
            result = await processor.process_incident(incident)
            summary = result.summary
            assert summary is not None
            rows.append(
                {
                    "incident": incident.number,
                    "sop": summary.sop_id or "(none)",
                    "outcome": summary.outcome_label,
                    "verified_by": summary.verified_by,
                    "reason": summary.escalation_reason,
                }
            )
            print(f"\n--- {incident.number}: {incident.short_description}")
            print(f"  SOP:      {summary.sop_id or '(no SOP matched)'}")
            print(f"  Outcome:  {summary.outcome_label.upper()}")
            if summary.verified_by:
                print(f"  Verified: step {summary.verified_by} passed")
            if summary.escalation_reason:
                print(f"  Reason:   {summary.escalation_reason}")
            for r in summary.step_results:
                first_line = r.output_summary.splitlines()[0] if r.output_summary else ""
                print(f"    [{r.status.value:9}] {r.step_id} ({r.step_type}) {first_line[:70]}")

        # A second delivery of the first incident is recognised and ignored.
        again = await processor.process_incident(Incident.from_servicenow(SAMPLE_INCIDENT))
        print(f"\n--- Redelivery of {again.incident_number}: {again.status.value} (no run created)")

    print(f"\nWork notes posted to the mock ServiceNow: {len(snow.work_notes)}")
    print("=" * 72 + "\n")
    return rows


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
