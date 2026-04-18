"""Unit tests for mock adapters."""

from __future__ import annotations

import pytest

from src.l1_agent.adapters.mock_adapters import (
    MockAutosysAdapter,
    MockIR360Adapter,
    MockSplunkAdapter,
    MockWindowsShareAdapter,
)


class TestMockSplunkAdapter:
    @pytest.mark.asyncio
    async def test_health_check(self):
        adapter = MockSplunkAdapter()
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_execute_returns_results(self):
        adapter = MockSplunkAdapter()
        result = await adapter.execute({"query": "index=main error"})
        assert result.success is True
        assert result.data["result_count"] > 0
        assert "Splunk search returned" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_adapter_name(self):
        adapter = MockSplunkAdapter()
        assert adapter.adapter_name == "MockSplunk"


class TestMockIR360Adapter:
    @pytest.mark.asyncio
    async def test_health_check(self):
        adapter = MockIR360Adapter()
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_depth_check(self):
        adapter = MockIR360Adapter()
        result = await adapter.execute({
            "queue_manager": "QMPROD01",
            "queue": "PAYMENT.REQUEST",
            "action": "depth",
        })
        assert result.success is True
        assert result.data["current_depth"] == 1523
        assert "depth" in result.evidence_snippet.lower()

    @pytest.mark.asyncio
    async def test_status_check(self):
        adapter = MockIR360Adapter()
        result = await adapter.execute({
            "queue_manager": "QMPROD01",
            "queue": "PAYMENT.REQUEST",
            "action": "status",
        })
        assert result.success is True
        assert result.data["status"] == "RUNNING"


class TestMockWindowsShareAdapter:
    @pytest.mark.asyncio
    async def test_health_check(self):
        adapter = MockWindowsShareAdapter()
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_read_lines(self):
        adapter = MockWindowsShareAdapter()
        result = await adapter.execute({
            "unc_path": "//server/logs/app.log",
        })
        assert result.success is True
        assert result.data["line_count"] > 0

    @pytest.mark.asyncio
    async def test_pattern_filter(self):
        adapter = MockWindowsShareAdapter()
        result = await adapter.execute({
            "unc_path": "//server/logs/app.log",
            "pattern": "ERROR",
        })
        assert result.success is True
        # Filtered results should only have ERROR lines
        for line in result.raw_output.split("\n"):
            if line.strip():
                assert "ERROR" in line or "FATAL" in line


class TestMockAutosysAdapter:
    @pytest.mark.asyncio
    async def test_health_check(self):
        adapter = MockAutosysAdapter()
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_status_query(self):
        adapter = MockAutosysAdapter()
        result = await adapter.execute({
            "job_name": "TEST_JOB",
            "query_type": "status",
        })
        assert result.success is True
        assert len(result.data["jobs"]) > 0

    @pytest.mark.asyncio
    async def test_dependencies_query(self):
        adapter = MockAutosysAdapter()
        result = await adapter.execute({
            "job_name": "TEST_JOB",
            "query_type": "dependencies",
        })
        assert result.success is True
        assert len(result.data["jobs"]) > 0
