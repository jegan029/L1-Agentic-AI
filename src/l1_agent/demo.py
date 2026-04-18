"""Demo mode: process a sample incident with a sample SOP using mock adapters.

Runs both the AI-driven flow (using a mock LLM) and the rule-based fallback
to demonstrate the full agent capabilities.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.l1_agent.adapters.mock_adapters import (
    MockAutosysAdapter,
    MockDynatraceAdapter,
    MockIR360Adapter,
    MockMainframeAdapter,
    MockSplunkAdapter,
    MockWebUIScraperAdapter,
    MockWindowsShareAdapter,
)
from src.l1_agent.ai.ai_executor import AIExecutor
from src.l1_agent.ai.analyzer import AIAnalyzer
from src.l1_agent.ai.mock_llm import MockLLMClient
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.utils.logging import get_logger, setup_logging

logger = get_logger("demo")

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


def _load_sample_sop() -> SOP:
    """Load sample SOP from file or use inline definition."""
    sample_sop_path = Path(__file__).parent.parent.parent / "data" / "sample_sops" / "mq_queue_depth_high.json"
    if sample_sop_path.exists():
        with open(sample_sop_path) as f:
            return SOP.from_dict(json.load(f))

    # Inline fallback
    return SOP.from_dict({
        "sop_id": "SOP-MQ-001",
        "title": "MQ Queue Depth High - Investigation and Remediation",
        "applicable_services": ["PaymentService", "OrderService"],
        "applicable_categories": ["Middleware", "MQ"],
        "applicable_assignment_groups": ["L1-Middleware-Support"],
        "keywords": ["queue depth", "mq", "messages not consumed", "queue high", "payment"],
        "steps": [
            {
                "step_id": "step-1",
                "step_type": "NOTE",
                "description": "Begin MQ queue depth investigation",
                "parameters": {"text": "Starting investigation of MQ queue depth issue."},
            },
            {
                "step_id": "step-2",
                "step_type": "MQ_CHECK",
                "description": "Check queue depth on reported queue",
                "parameters": {
                    "queue_manager": "QMPROD01",
                    "queue": "PAYMENT.REQUEST",
                    "action": "depth",
                },
                "expected_output": "Queue depth and consumer status",
                "on_success": "step-3",
                "on_failure": "step-escalate",
            },
            {
                "step_id": "step-3",
                "step_type": "MQ_CHECK",
                "description": "Check queue status and consumers",
                "parameters": {
                    "queue_manager": "QMPROD01",
                    "queue": "PAYMENT.REQUEST",
                    "action": "status",
                },
                "on_success": "step-4",
                "on_failure": "step-escalate",
            },
            {
                "step_id": "step-4",
                "step_type": "SPLUNK_SEARCH",
                "description": "Search application logs for errors",
                "parameters": {
                    "query": 'index=app_logs sourcetype=log4j host=app-server-* "PaymentService" (ERROR OR FATAL)',
                    "time_range": {"earliest": "-2h", "latest": "now"},
                },
                "on_success": "step-5",
                "on_failure": "step-escalate",
            },
            {
                "step_id": "step-5",
                "step_type": "AUTOSYS_STATUS",
                "description": "Check if batch payment processing job is running",
                "parameters": {
                    "job_name": "BATCH_PAYMENT_PROCESS",
                    "query_type": "status",
                },
                "on_success": "step-6",
                "on_failure": "step-escalate",
            },
            {
                "step_id": "step-6",
                "step_type": "DECISION",
                "description": "Evaluate all check results",
                "parameters": {"rule": "any_failed"},
                "on_success": "step-resolve",
                "on_failure": "step-escalate",
            },
            {
                "step_id": "step-resolve",
                "step_type": "NOTE",
                "description": "All checks passed - summarise findings",
                "parameters": {
                    "text": (
                        "Investigation complete. All checks executed successfully. "
                        "Evidence collected and attached. Queue depth is elevated but "
                        "consumers are being investigated. Recommend monitoring."
                    ),
                },
            },
            {
                "step_id": "step-escalate",
                "step_type": "NOTE",
                "description": "Escalation required",
                "parameters": {
                    "text": (
                        "One or more checks failed or produced concerning results. "
                        "Escalating to L2 with collected evidence."
                    ),
                },
            },
        ],
        "pre_checks": ["Verify MQ connectivity", "Verify Splunk access"],
        "tools_required": ["IR360", "Splunk", "Autosys"],
        "escalation_criteria": {
            "max_retry_count": 2,
            "escalate_on_access_denied": True,
            "escalate_on_ambiguous_result": True,
            "escalate_on_write_action": True,
        },
    })


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


def _print_summary(summary: object, mock_snow: MockServiceNowClient) -> None:
    """Print execution summary and work notes."""
    from src.l1_agent.models.evidence import ExecutionSummary

    assert isinstance(summary, ExecutionSummary)

    print("\n--- Execution Summary ---")
    print(f"  Outcome:     {summary.outcome.value.upper()}")
    print(f"  Steps run:   {len(summary.step_results)}")
    print(f"  Duration:    {summary.total_duration_ms:.0f}ms")
    if summary.escalation_reason:
        print(f"  Escalation:  {summary.escalation_reason}")

    print("\n--- Step Results ---")
    for r in summary.step_results:
        status_icon = {
            "success": "OK", "fail": "FAIL",
            "skip": "SKIP", "escalated": "ESC",
        }.get(r.status.value, "?")
        print(f"  [{status_icon}] {r.step_id} ({r.step_type})")
        if r.evidence:
            for line in r.evidence.split("\n")[:3]:
                print(f"        {line}")

    print(f"\n--- Work Notes Posted ({len(mock_snow.work_notes)}) ---")
    for i, wn in enumerate(mock_snow.work_notes):
        preview = wn["note"][:120].replace("\n", " | ")
        print(f"  [{i+1}] {preview}...")

    print("\n--- Incident Update Payload ---")
    update_payload = {
        "work_notes": summary.to_work_note(),
        "state": "6" if summary.outcome.value == "resolved" else "2",
    }
    print(json.dumps(update_payload, indent=2)[:1000])


async def run_demo() -> None:
    """Run the demo scenario end-to-end."""
    setup_logging("INFO")
    logger.info("=== L1 Virtual Engineer Agent - Demo Mode ===")

    # Shared mock adapters
    adapters = {
        "splunk": MockSplunkAdapter(),
        "ir360": MockIR360Adapter(),
        "windows_share": MockWindowsShareAdapter(),
        "autosys": MockAutosysAdapter(),
        "dynatrace": MockDynatraceAdapter(),
        "webui": MockWebUIScraperAdapter(),
        "mainframe": MockMainframeAdapter(),
    }

    # Load sample data
    incident = Incident.from_servicenow(SAMPLE_INCIDENT)
    sop = _load_sample_sop()

    print("\n" + "=" * 70)
    print("  L1 VIRTUAL ENGINEER AGENT - DEMO")
    print("=" * 70)

    # Show incident
    print("\n--- Incident Received ---")
    print(f"  Number:      {incident.number}")
    print(f"  Description: {incident.short_description}")
    print(f"  Category:    {incident.category}/{incident.subcategory}")
    print(f"  CI:          {incident.cmdb_ci}")
    print(f"  Priority:    {incident.priority}")

    # ═══════════════════════════════════════════════════════════════════
    #  PART 1: AI-DRIVEN MODE (Mock LLM)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  PART 1: AI-DRIVEN MODE (using Mock LLM)")
    print("=" * 70)

    mock_snow_ai = MockServiceNowClient()
    mock_llm = MockLLMClient()  # auto-generates realistic tool calls
    ai_analyzer = AIAnalyzer(llm_client=mock_llm)
    ai_executor = AIExecutor(
        llm_client=mock_llm,
        adapters=adapters,
    )

    # AI SOP selection
    print("\n--- AI Incident Analysis ---")
    analysis = await ai_analyzer.analyze_incident(incident)
    print(f"  {analysis[:200]}")

    print("\n--- AI SOP Selection ---")
    ai_match = await ai_analyzer.select_sop(incident, [sop])
    print(f"  Selected SOP: {ai_match.sop.title if ai_match.sop else 'None'}")
    print(f"  Confidence:   {ai_match.confidence:.2f}")
    print(f"  AI Rationale: {ai_match.rationale[:200]}")

    # AI-driven execution (LLM decides which tools to call)
    print("\n--- AI-Driven SOP Execution ---")
    print("  (The LLM autonomously calls tools and interprets results)")
    ai_summary = await ai_executor.execute_sop(
        incident,
        sop,
        work_note_callback=mock_snow_ai.add_work_note,
    )
    _print_summary(ai_summary, mock_snow_ai)

    # ═══════════════════════════════════════════════════════════════════
    #  PART 2: RULE-BASED MODE (Fallback)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  PART 2: RULE-BASED MODE (fallback, no LLM)")
    print("=" * 70)

    mock_snow_rule = MockServiceNowClient()
    executor = SOPExecutor(adapters=adapters)
    matcher = SOPMatcher(confidence_threshold=0.6)

    # Rule-based SOP matching
    match_result = matcher.match(incident, [sop])
    print("\n--- Rule-Based SOP Matching ---")
    print(f"  Matched SOP: {match_result.sop.title if match_result.sop else 'None'}")
    print(f"  Confidence:  {match_result.confidence:.2f}")
    print(f"  Rationale:   {match_result.rationale}")

    # Rule-based execution
    print("\n--- Rule-Based SOP Execution ---")
    rule_summary = await executor.execute_sop(
        incident,
        sop,
        work_note_callback=mock_snow_rule.add_work_note,
    )
    _print_summary(rule_summary, mock_snow_rule)

    print("\n" + "=" * 70)
    print("  Demo complete. Both AI-driven and rule-based modes demonstrated.")
    print("=" * 70 + "\n")


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
