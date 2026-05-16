"""Tests for the Dynatrace server-memory-high SOP scenario (SOP-INFRA-001).

Covers:
- MockDynatraceAdapter returns memory-alert data for the prod host
- MockDynatraceAdapter stays healthy for all other hosts (regression guard)
- SOP file can be loaded and passes basic structure checks
- SOPMatcher scores the memory incident above the confidence threshold
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.l1_agent.adapters.mock_adapters import MockDynatraceAdapter
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.models.incident import Incident


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def adapter():
    return MockDynatraceAdapter()


@pytest.fixture
def memory_sop():
    sop_path = (
        Path(__file__).resolve().parent.parent.parent
        / "data" / "sample_sops" / "server_memory_high.json"
    )
    return json.loads(sop_path.read_text(encoding="utf-8"))


@pytest.fixture
def parsed_memory_sop(memory_sop):
    return SOPParser().parse_from_json(memory_sop)


@pytest.fixture
def memory_incident():
    return Incident(
        sys_id="abc123",
        number="INC0070001",
        short_description="High memory usage alert on app-server-prod01 - memory at 92%",
        description=(
            "Dynatrace has triggered a memory saturation alert for app-server-prod01. "
            "Memory usage is at 92% of total 32GB. Application response times increasing. "
            "OOM risk detected."
        ),
        category="Infrastructure",
        subcategory="Server",
        cmdb_ci="AppServerProd01",
        assignment_group="L1-Infra-Support",
        priority=2,
        state=1,
    )


# ── Mock adapter: memory-alert host ───────────────────────────────────────────

class TestDynatraceMemoryAlertHost:
    """MockDynatraceAdapter returns elevated data for the prod memory-alert host."""

    @pytest.mark.asyncio
    async def test_vm_health_shows_problem_for_prod_host(self, adapter):
        result = await adapter.execute({
            "action": "vm_health",
            "host_name": "app-server-prod01",
        })
        assert result.success is True
        assert result.data["hosts_found"] == 1
        assert result.data["healthy"] is False
        assert result.data["problems_count"] == 1
        problem = result.data["problems"][0]
        assert problem["severityLevel"] == "PERFORMANCE"
        assert "memory" in problem["title"].lower()

    @pytest.mark.asyncio
    async def test_vm_health_evidence_shows_active_problem(self, adapter):
        result = await adapter.execute({
            "action": "vm_health",
            "host_name": "app-server-prod01",
        })
        assert "Active problems: 1" in result.evidence_snippet
        assert "Memory saturation" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_problems_shows_memory_saturation_for_prod_entity(self, adapter):
        result = await adapter.execute({
            "action": "problems",
            "entity_selector": 'entityName("app-server-prod01")',
        })
        assert result.success is True
        assert result.data["problem_count"] == 1
        assert result.data["problems"][0]["title"] == "Memory saturation"

    @pytest.mark.asyncio
    async def test_metrics_shows_critical_memory(self, adapter):
        result = await adapter.execute({
            "action": "metrics",
            "metric_selector": "builtin:host.mem.usage,builtin:host.mem.availableBytes",
            "entity_selector": 'type("HOST"),entityName("app-server-prod01")',
            "time_range": "now-1h",
        })
        assert result.success is True
        mem_metric = next(
            m for m in result.data["metrics"]
            if m["metric_id"] == "builtin:host.mem.usage"
        )
        assert mem_metric["latest_value"] == 92.5
        assert "92.5" in result.evidence_snippet
        assert "CRITICAL" in result.evidence_snippet


# ── Mock adapter: regression guard — other hosts stay healthy ─────────────────

class TestDynatraceHealthyHostRegression:
    """Existing test hosts must still be reported as healthy."""

    @pytest.mark.asyncio
    async def test_web_server_remains_healthy(self, adapter):
        result = await adapter.execute({
            "action": "vm_health",
            "host_name": "web-server-01",
        })
        assert result.data["healthy"] is True
        assert result.data["problems_count"] == 0

    @pytest.mark.asyncio
    async def test_app_server_01_remains_healthy(self, adapter):
        result = await adapter.execute({
            "action": "vm_health",
            "host_name": "app-server-01",
        })
        assert result.data["healthy"] is True
        assert result.data["problems_count"] == 0

    @pytest.mark.asyncio
    async def test_problems_zero_for_unrelated_entity_selector(self, adapter):
        result = await adapter.execute({
            "action": "problems",
            "entity_selector": 'entityName("web-server-01")',
        })
        assert result.data["problem_count"] == 0

    @pytest.mark.asyncio
    async def test_problems_zero_when_no_entity_selector(self, adapter):
        result = await adapter.execute({"action": "problems"})
        assert result.data["problem_count"] == 0


# ── SOP structure ─────────────────────────────────────────────────────────────

class TestMemorySopStructure:
    """SOP-INFRA-001 JSON has the expected shape and step types."""

    def test_sop_id_and_title(self, memory_sop):
        assert memory_sop["sop_id"] == "SOP-INFRA-001"
        assert "memory" in memory_sop["title"].lower()
        assert "dynatrace" in memory_sop["title"].lower()

    def test_tools_required_includes_dynatrace(self, memory_sop):
        assert "Dynatrace" in memory_sop["tools_required"]

    def test_step_types_include_dynatrace(self, memory_sop):
        step_types = {s["step_type"] for s in memory_sop["steps"]}
        assert "DYNATRACE_VM_CHECK" in step_types
        assert "DYNATRACE_METRICS" in step_types

    def test_step_types_include_splunk_and_decision(self, memory_sop):
        step_types = {s["step_type"] for s in memory_sop["steps"]}
        assert "SPLUNK_SEARCH" in step_types
        assert "DECISION" in step_types

    def test_vm_health_step_targets_prod_host(self, memory_sop):
        vm_step = next(
            s for s in memory_sop["steps"]
            if s["step_type"] == "DYNATRACE_VM_CHECK"
            and s["parameters"].get("action") == "vm_health"
        )
        assert vm_step["parameters"]["host_name"] == "app-server-prod01"

    def test_metrics_step_queries_memory_selector(self, memory_sop):
        metrics_step = next(
            s for s in memory_sop["steps"]
            if s["step_type"] == "DYNATRACE_METRICS"
        )
        selector = metrics_step["parameters"]["metric_selector"]
        assert "mem.usage" in selector

    def test_parsed_sop_has_correct_step_count(self, parsed_memory_sop):
        # 6 logic steps + 2 terminal notes = 8
        assert len(parsed_memory_sop.steps) == 8


# ── SOP matching ──────────────────────────────────────────────────────────────

class TestMemorySopMatching:
    """SOPMatcher selects SOP-INFRA-001 for a memory incident with high confidence."""

    def test_memory_incident_matches_above_threshold(
        self, parsed_memory_sop, memory_incident
    ):
        matcher = SOPMatcher(confidence_threshold=0.6)
        result = matcher.match(memory_incident, [parsed_memory_sop])
        assert result.sop is not None
        assert result.sop.sop_id == "SOP-INFRA-001"
        assert result.confidence >= 0.6

    def test_memory_incident_not_matched_to_mq_sop(self, memory_incident):
        """Verify the memory incident does NOT match the MQ SOP."""
        mq_sop_path = (
            Path(__file__).resolve().parent.parent.parent
            / "data" / "sample_sops" / "mq_queue_depth_high.json"
        )
        mq_sop = SOPParser().parse_from_json(
            json.loads(mq_sop_path.read_text(encoding="utf-8"))
        )
        matcher = SOPMatcher(confidence_threshold=0.6)
        result = matcher.match(memory_incident, [mq_sop])
        # Either no match or very low confidence
        assert result.confidence < 0.6

    def test_memory_sop_wins_over_all_sops(
        self, parsed_memory_sop, memory_incident
    ):
        """When all three SOPs are present, SOP-INFRA-001 wins for the memory incident."""
        mq_path = (
            Path(__file__).resolve().parent.parent.parent
            / "data" / "sample_sops" / "mq_queue_depth_high.json"
        )
        autosys_path = (
            Path(__file__).resolve().parent.parent.parent
            / "data" / "sample_sops" / "autosys_job_failure.json"
        )
        parser = SOPParser()
        all_sops = [
            parsed_memory_sop,
            parser.parse_from_json(json.loads(mq_path.read_text(encoding="utf-8"))),
            parser.parse_from_json(json.loads(autosys_path.read_text(encoding="utf-8"))),
        ]
        matcher = SOPMatcher(confidence_threshold=0.6)
        result = matcher.match(memory_incident, all_sops)
        assert result.sop is not None
        assert result.sop.sop_id == "SOP-INFRA-001"
        assert result.confidence >= 0.6
