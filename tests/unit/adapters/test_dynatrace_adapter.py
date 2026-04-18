"""Unit tests for DynatraceAdapter and MockDynatraceAdapter."""

from __future__ import annotations

import pytest

from src.l1_agent.adapters.mock_adapters import MockDynatraceAdapter


@pytest.fixture
def adapter():
    return MockDynatraceAdapter()


class TestMockDynatraceAdapter:
    """Tests for MockDynatraceAdapter (used in demo mode and CI)."""

    @pytest.mark.asyncio
    async def test_adapter_name(self, adapter):
        assert adapter.adapter_name == "MockDynatrace"

    @pytest.mark.asyncio
    async def test_health_check(self, adapter):
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_vm_health_check(self, adapter):
        result = await adapter.execute({
            "action": "vm_health",
            "host_name": "web-server-01",
        })
        assert result.success is True
        assert result.data["hosts_found"] == 1
        assert result.data["healthy"] is True
        assert result.data["problems_count"] == 0
        assert "web-server-01" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_vm_health_default_action(self, adapter):
        """Default action should be vm_health."""
        result = await adapter.execute({"host_name": "app-server-01"})
        assert result.success is True
        assert result.data["healthy"] is True

    @pytest.mark.asyncio
    async def test_metrics_check(self, adapter):
        result = await adapter.execute({
            "action": "metrics",
            "metric_selector": "builtin:host.cpu.usage",
        })
        assert result.success is True
        assert result.data["metric_count"] == 2
        assert len(result.data["metrics"]) == 2
        # Check CPU metric present
        cpu_metric = next(
            m for m in result.data["metrics"]
            if m["metric_id"] == "builtin:host.cpu.usage"
        )
        assert cpu_metric["latest_value"] == 42.3

    @pytest.mark.asyncio
    async def test_problems_check(self, adapter):
        result = await adapter.execute({"action": "problems"})
        assert result.success is True
        assert result.data["problem_count"] == 0
        assert result.data["problems"] == []

    @pytest.mark.asyncio
    async def test_unknown_action(self, adapter):
        result = await adapter.execute({"action": "nonexistent"})
        assert result.success is False
        assert "Unknown action" in result.error

    @pytest.mark.asyncio
    async def test_evidence_snippet_contains_host(self, adapter):
        result = await adapter.execute({
            "action": "vm_health",
            "host_name": "my-special-host",
        })
        assert "my-special-host" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_metrics_evidence_snippet(self, adapter):
        result = await adapter.execute({"action": "metrics"})
        assert "cpu.usage" in result.evidence_snippet
        assert "mem.usage" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_raw_output_is_json(self, adapter):
        import json
        result = await adapter.execute({"action": "vm_health", "host_name": "h1"})
        parsed = json.loads(result.raw_output)
        assert "hosts" in parsed
