"""Unit tests for MainframeAdapter and MockMainframeAdapter."""

from __future__ import annotations

import pytest

from src.l1_agent.adapters.mock_adapters import MockMainframeAdapter


@pytest.fixture
def adapter():
    return MockMainframeAdapter()


class TestMockMainframeAdapter:
    """Tests for MockMainframeAdapter (used in demo mode and CI)."""

    @pytest.mark.asyncio
    async def test_adapter_name(self, adapter):
        assert adapter.adapter_name == "MockMainframe"

    @pytest.mark.asyncio
    async def test_health_check(self, adapter):
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_async_status_check(self, adapter):
        result = await adapter.execute({
            "action": "async_status",
            "job_name": "BEIM_ASYNC_JOB01",
            "expected_status": "inact ok",
        })
        assert result.success is True
        assert result.data["all_match_expected"] is True
        assert result.data["jobs_checked"] == 2
        assert result.data["expected_status"] == "inact ok"

    @pytest.mark.asyncio
    async def test_async_status_default_action(self, adapter):
        """Default action should be async_status."""
        result = await adapter.execute({
            "job_name": "MY_JOB",
        })
        assert result.success is True
        assert result.data["all_match_expected"] is True

    @pytest.mark.asyncio
    async def test_job_status_check(self, adapter):
        result = await adapter.execute({
            "action": "job_status",
            "job_name": "BEIM_BATCH_01",
            "expected_status": "inact ok",
        })
        assert result.success is True
        assert result.data["jobs_checked"] == 2

    @pytest.mark.asyncio
    async def test_screen_check(self, adapter):
        result = await adapter.execute({
            "action": "screen_check",
            "expected_text": "BEIM",
        })
        assert result.success is True
        assert result.data["text_found"] is True
        assert "BEIM" in result.data["screen_text"]

    @pytest.mark.asyncio
    async def test_screen_check_text_not_found(self, adapter):
        result = await adapter.execute({
            "action": "screen_check",
            "expected_text": "NONEXISTENT_TEXT_12345",
        })
        assert result.success is False
        assert result.data["text_found"] is False

    @pytest.mark.asyncio
    async def test_screen_check_no_expected_text(self, adapter):
        result = await adapter.execute({
            "action": "screen_check",
        })
        assert result.success is True
        assert result.data["text_found"] is True

    @pytest.mark.asyncio
    async def test_evidence_contains_host(self, adapter):
        result = await adapter.execute({
            "action": "async_status",
            "job_name": "TEST_JOB",
        })
        assert "mainframe-prod" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_evidence_contains_job_name(self, adapter):
        result = await adapter.execute({
            "action": "async_status",
            "job_name": "MY_SPECIAL_JOB",
        })
        assert "MY_SPECIAL_JOB" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_evidence_contains_inact_ok(self, adapter):
        result = await adapter.execute({
            "action": "async_status",
            "job_name": "BEIM_ASYNC_JOB01",
        })
        assert "inact ok" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_raw_output_is_json(self, adapter):
        import json
        result = await adapter.execute({
            "action": "async_status",
            "job_name": "J1",
        })
        parsed = json.loads(result.raw_output)
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    @pytest.mark.asyncio
    async def test_screen_text_contains_beim_content(self, adapter):
        result = await adapter.execute({"action": "screen_check"})
        screen = result.data["screen_text"]
        assert "CICS BEIM STATUS DISPLAY" in screen
        assert "BEIM_ASYNC_JOB01" in screen
        assert "INACT OK" in screen


class TestMainframeBeimScreenParser:
    """Tests for the BEIM screen parser function."""

    def test_parse_beim_screen_basic(self):
        from src.l1_agent.adapters.mainframe_adapter import _parse_beim_screen
        screen = (
            "ASYNC JOBS:\n"
            "  JOB01    INACT OK     LAST RUN: 04/14/26 09:00\n"
            "  JOB02    ACTIVE       STARTED:  04/14/26 09:50\n"
            "  JOB03    INACT ERROR  LAST RUN: 04/14/26 08:30\n"
        )
        jobs = _parse_beim_screen(screen)
        assert len(jobs) == 3
        assert jobs[0]["job_name"] == "JOB01"
        assert jobs[0]["status"] == "inact ok"
        assert jobs[1]["status"] == "active"
        assert jobs[2]["status"] == "inact error"

    def test_parse_beim_screen_with_filter(self):
        from src.l1_agent.adapters.mainframe_adapter import _parse_beim_screen
        screen = (
            "  ALPHA_JOB    INACT OK\n"
            "  BETA_JOB     ACTIVE\n"
        )
        jobs = _parse_beim_screen(screen, job_filter="ALPHA")
        assert len(jobs) == 1
        assert jobs[0]["job_name"] == "ALPHA_JOB"

    def test_parse_beim_screen_empty(self):
        from src.l1_agent.adapters.mainframe_adapter import _parse_beim_screen
        jobs = _parse_beim_screen("no jobs here")
        assert jobs == []

    def test_parse_beim_screen_case_insensitive(self):
        from src.l1_agent.adapters.mainframe_adapter import _parse_beim_screen
        screen = "  MYJOB    inact ok\n"
        jobs = _parse_beim_screen(screen)
        assert len(jobs) == 1
        assert jobs[0]["status"] == "inact ok"
