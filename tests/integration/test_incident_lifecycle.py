"""Integration tests for the full incident lifecycle.

These tests exercise the complete incident processing pipeline using mock
adapters and mock LLM — no real external systems required.

Run with:
    pytest tests/integration/ -v
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.config.settings import AgentSettings, Settings
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.models.evidence import ExecutionOutcome
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.utils.retry import CircuitBreaker

DATA_DIR = os.path.join(os.path.dirname(__file__), "../../data/sample_sops")


def load_sample_sop(filename: str) -> SOP:
    parser = SOPParser()
    path = os.path.join(DATA_DIR, filename)
    with open(path) as f:
        raw = json.load(f)
    sop = parser.parse(raw)
    assert sop is not None, f"Failed to parse SOP from {filename}"
    return sop


def make_incident(**kwargs: Any) -> Incident:
    defaults = dict(
        sys_id="SYS001",
        number="INC0000001",
        short_description="MQ queue depth high on QMPROD01",
        description="Payment queue PAYMENT.REQUEST depth has exceeded threshold.",
        category="Middleware",
        subcategory="MQ",
        cmdb_ci="PaymentService",
        assignment_group="L1-Middleware-Support",
        priority=2,
        state=1,
    )
    defaults.update(kwargs)
    return Incident(**defaults)


def make_processor(
    sop_cache: List[SOP],
    snow_client: ServiceNowClient,
    use_ai: bool = False,
) -> IncidentProcessor:
    adapters = {
        "splunk": MockSplunkAdapter(),
        "ir360": MockIR360Adapter(),
        "windows_share": MockWindowsShareAdapter(),
        "autosys": MockAutosysAdapter(),
        "dynatrace": MockDynatraceAdapter(),
        "webui": MockWebUIScraperAdapter(),
        "mainframe": MockMainframeAdapter(),
    }
    cb_args = {"failure_threshold": 5, "reset_timeout_seconds": 60}
    circuit_breakers = {name: CircuitBreaker(**cb_args) for name in adapters}
    executor = SOPExecutor(
        adapters=adapters,
        circuit_breakers=circuit_breakers,
        retry_max_attempts=1,
        retry_base_delay=0.01,
    )
    matcher = SOPMatcher(confidence_threshold=0.3)
    parser = SOPParser()

    llm_client = None
    ai_analyzer = None
    ai_executor = None
    if use_ai:
        llm_client = MockLLMClient()
        ai_analyzer = AIAnalyzer(llm_client)
        ai_executor = AIExecutor(llm_client=llm_client, adapters=adapters)

    return IncidentProcessor(
        snow_client=snow_client,
        sop_matcher=matcher,
        sop_parser=parser,
        executor=executor,
        sop_cache=sop_cache,
        confidence_threshold=0.3,
        llm_client=llm_client,
        ai_analyzer=ai_analyzer,
        ai_executor=ai_executor,
    )


def make_mock_snow() -> ServiceNowClient:
    snow = MagicMock(spec=ServiceNowClient)
    snow.add_work_note = AsyncMock(return_value=True)
    snow.update_incident = AsyncMock(return_value=True)
    snow.get_sops = AsyncMock(return_value=[])
    return snow


# ── Tests ─────────────────────────────────────────────────────────────


class TestRuleBasedMQIncident:
    """End-to-end rule-based processing for an MQ queue depth incident."""

    @pytest.fixture
    def mq_sop(self) -> SOP:
        return load_sample_sop("mq_queue_depth_high.json")

    @pytest.mark.asyncio
    async def test_resolves_mq_incident(self, mq_sop: SOP) -> None:
        snow = make_mock_snow()
        incident = make_incident()
        processor = make_processor([mq_sop], snow, use_ai=False)

        summary = await processor.process_incident(incident)

        assert summary.incident_number == "INC0000001"
        assert summary.sop_id == "SOP-MQ-001"
        assert summary.outcome in (ExecutionOutcome.RESOLVED, ExecutionOutcome.ESCALATED)
        # Work notes must be posted
        assert snow.add_work_note.call_count >= 1

    @pytest.mark.asyncio
    async def test_idempotency_skips_duplicate(self, mq_sop: SOP) -> None:
        snow = make_mock_snow()
        incident = make_incident()
        processor = make_processor([mq_sop], snow, use_ai=False)

        await processor.process_incident(incident)
        first_call_count = snow.add_work_note.call_count

        # Process same incident again — should be a no-op
        await processor.process_incident(incident)
        assert snow.add_work_note.call_count == first_call_count

    @pytest.mark.asyncio
    async def test_escalates_when_no_sop(self) -> None:
        snow = make_mock_snow()
        incident = make_incident(
            short_description="Unknown error XYZ",
            description="Something weird happened.",
            category="Unknown",
            cmdb_ci="UnknownService",
        )
        processor = make_processor([], snow, use_ai=False)

        summary = await processor.process_incident(incident)

        assert summary.outcome == ExecutionOutcome.ESCALATED
        assert "No" in summary.escalation_reason or "no" in summary.escalation_reason.lower()

    @pytest.mark.asyncio
    async def test_escalates_low_confidence(self, mq_sop: SOP) -> None:
        snow = make_mock_snow()
        incident = make_incident(
            short_description="database issue",
            description="slow queries",
            category="Database",
            cmdb_ci="DatabaseService",
        )
        processor = IncidentProcessor(
            snow_client=snow,
            sop_matcher=SOPMatcher(confidence_threshold=0.95),
            sop_parser=SOPParser(),
            executor=SOPExecutor(
                adapters={},
                circuit_breakers={},
                retry_max_attempts=1,
                retry_base_delay=0.01,
            ),
            sop_cache=[mq_sop],
            confidence_threshold=0.95,
        )

        summary = await processor.process_incident(incident)
        assert summary.outcome == ExecutionOutcome.ESCALATED


class TestRuleBasedAutosysIncident:
    """End-to-end rule-based processing for an Autosys job failure incident."""

    @pytest.fixture
    def autosys_sop(self) -> SOP:
        return load_sample_sop("autosys_job_failure.json")

    @pytest.mark.asyncio
    async def test_processes_autosys_incident(self, autosys_sop: SOP) -> None:
        snow = make_mock_snow()
        incident = make_incident(
            number="INC0000002",
            sys_id="SYS002",
            short_description="Autosys batch job BATCH_001 failed overnight",
            description="Job BATCH_001 has status FAILURE with exit code 127.",
            category="Batch",
            cmdb_ci="BatchService",
        )
        processor = make_processor([autosys_sop], snow, use_ai=False)

        summary = await processor.process_incident(incident)

        assert summary.incident_number == "INC0000002"
        assert summary.outcome in (ExecutionOutcome.RESOLVED, ExecutionOutcome.ESCALATED)
        assert snow.add_work_note.call_count >= 1

    @pytest.mark.asyncio
    async def test_multi_sop_selects_best_match(self) -> None:
        mq_sop = load_sample_sop("mq_queue_depth_high.json")
        autosys_sop = load_sample_sop("autosys_job_failure.json")
        snow = make_mock_snow()
        incident = make_incident(
            short_description="MQ queue depth high on QMPROD01",
            category="Middleware",
            cmdb_ci="PaymentService",
        )
        processor = make_processor([mq_sop, autosys_sop], snow, use_ai=False)

        summary = await processor.process_incident(incident)

        assert summary.sop_id == "SOP-MQ-001"


class TestAIDrivenProcessing:
    """End-to-end AI-driven processing with MockLLMClient."""

    @pytest.fixture
    def mq_sop(self) -> SOP:
        return load_sample_sop("mq_queue_depth_high.json")

    @pytest.mark.asyncio
    async def test_ai_mode_processes_incident(self, mq_sop: SOP) -> None:
        snow = make_mock_snow()
        incident = make_incident()
        processor = make_processor([mq_sop], snow, use_ai=True)

        assert processor.ai_enabled
        summary = await processor.process_incident(incident)

        assert summary.incident_number == "INC0000001"
        assert summary.outcome in (ExecutionOutcome.RESOLVED, ExecutionOutcome.ESCALATED, ExecutionOutcome.PARTIAL)
        # AI mode posts at least the initial analysis note
        assert snow.add_work_note.call_count >= 1

    @pytest.mark.asyncio
    async def test_ai_mode_idempotency(self, mq_sop: SOP) -> None:
        snow = make_mock_snow()
        incident = make_incident(sys_id="SYS_AI_001", number="INC9999001")
        processor = make_processor([mq_sop], snow, use_ai=True)

        await processor.process_incident(incident)
        call_count_after_first = snow.add_work_note.call_count

        await processor.process_incident(incident)
        assert snow.add_work_note.call_count == call_count_after_first


class TestSOPMatchingIntegration:
    """Integration tests for SOP matching logic."""

    @pytest.mark.asyncio
    async def test_keyword_match_drives_sop_selection(self) -> None:
        mq_sop = load_sample_sop("mq_queue_depth_high.json")
        snow = make_mock_snow()
        incident = make_incident(
            short_description="queue depth exceeded threshold on payment queue",
            category="Middleware",
        )
        processor = make_processor([mq_sop], snow, use_ai=False)
        summary = await processor.process_incident(incident)
        assert summary.sop_id == "SOP-MQ-001"

    @pytest.mark.asyncio
    async def test_work_notes_contain_sop_info(self) -> None:
        mq_sop = load_sample_sop("mq_queue_depth_high.json")
        snow = make_mock_snow()
        incident = make_incident()
        processor = make_processor([mq_sop], snow, use_ai=False)
        await processor.process_incident(incident)

        all_notes = " ".join(
            str(call.args[1]) for call in snow.add_work_note.call_args_list
        )
        assert "SOP" in all_notes or "mq" in all_notes.lower() or "queue" in all_notes.lower()


class TestAdapterHealthChecks:
    """Verify all mock adapters pass health checks."""

    @pytest.mark.asyncio
    async def test_all_mock_adapters_healthy(self) -> None:
        adapters = [
            MockSplunkAdapter(),
            MockIR360Adapter(),
            MockWindowsShareAdapter(),
            MockAutosysAdapter(),
            MockDynatraceAdapter(),
            MockWebUIScraperAdapter(),
            MockMainframeAdapter(),
        ]
        for adapter in adapters:
            result = await adapter.health_check()
            assert result is True, f"{adapter.adapter_name} health check failed"

    @pytest.mark.asyncio
    async def test_all_mock_adapters_execute(self) -> None:
        cases = [
            (MockSplunkAdapter(), {"query": "index=app_logs ERROR", "time_range": {"earliest": "-1h", "latest": "now"}}),
            (MockIR360Adapter(), {"queue_manager": "QM1", "queue": "TEST.Q", "action": "depth"}),
            (MockWindowsShareAdapter(), {"unc_path": r"\\server\share\app.log", "pattern": "ERROR"}),
            (MockAutosysAdapter(), {"job_name": "BATCH_001", "query_type": "status"}),
            (MockDynatraceAdapter(), {"host_name": "prod-host-01", "check_type": "health"}),
            (MockWebUIScraperAdapter(), {"url": "http://app.internal/health", "check_type": "page_load"}),
            (MockMainframeAdapter(), {"job_name": "ASYNCJOB1", "expected_status": "inact ok"}),
        ]
        for adapter, params in cases:
            result = await adapter.execute(params)
            assert result.success is True, f"{adapter.adapter_name}.execute() returned failure: {result.error}"
