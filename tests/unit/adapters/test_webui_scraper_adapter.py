"""Unit tests for WebUIScraperAdapter and MockWebUIScraperAdapter."""

from __future__ import annotations

import pytest

from src.l1_agent.adapters.mock_adapters import MockWebUIScraperAdapter


@pytest.fixture
def adapter():
    return MockWebUIScraperAdapter()


class TestMockWebUIScraperAdapter:
    """Tests for MockWebUIScraperAdapter (used in demo mode and CI)."""

    @pytest.mark.asyncio
    async def test_adapter_name(self, adapter):
        assert adapter.adapter_name == "MockWebUIScraper"

    @pytest.mark.asyncio
    async def test_health_check(self, adapter):
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_page_load_check(self, adapter):
        result = await adapter.execute({
            "url": "https://app.example.com/dashboard",
            "check_type": "page_load",
        })
        assert result.success is True
        assert result.data["status"] == "healthy"
        assert result.data["load_time_seconds"] > 0
        assert result.data["page_size_bytes"] > 0
        assert "app.example.com/dashboard" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_page_load_default_check_type(self, adapter):
        """Default check_type should be page_load."""
        result = await adapter.execute({"url": "https://example.com"})
        assert result.success is True
        assert result.data["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_page_load_with_expected_text(self, adapter):
        result = await adapter.execute({
            "url": "https://app.example.com",
            "check_type": "page_load",
            "expected_text": "Dashboard",
        })
        assert result.success is True
        assert "Dashboard" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_click_path_check(self, adapter):
        result = await adapter.execute({
            "url": "https://app.example.com",
            "check_type": "click_path",
            "click_steps": [
                {"action": "click", "selector": "#login-btn", "name": "click-login"},
                {"action": "wait", "value": "2", "name": "wait-page-load"},
                {"action": "assert_text", "value": "Welcome", "name": "verify-welcome"},
            ],
        })
        assert result.success is True
        assert result.data["all_passed"] is True
        # navigate + 3 steps
        assert result.data["steps_executed"] == 4
        assert result.data["steps_passed"] == 4
        assert result.data["total_time_seconds"] > 0

    @pytest.mark.asyncio
    async def test_click_path_empty_steps(self, adapter):
        result = await adapter.execute({
            "url": "https://app.example.com",
            "check_type": "click_path",
            "click_steps": [],
        })
        assert result.success is True
        # Only the navigate step
        assert result.data["steps_executed"] == 1

    @pytest.mark.asyncio
    async def test_evidence_contains_url(self, adapter):
        result = await adapter.execute({
            "url": "https://gcas-launcher.example.com",
            "check_type": "page_load",
        })
        assert "gcas-launcher.example.com" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_click_path_evidence_shows_steps(self, adapter):
        result = await adapter.execute({
            "url": "https://app.example.com",
            "check_type": "click_path",
            "click_steps": [
                {"action": "click", "selector": "#btn", "name": "my-step"},
            ],
        })
        assert "my-step" in result.evidence_snippet
        assert "[OK]" in result.evidence_snippet

    @pytest.mark.asyncio
    async def test_raw_output_is_json(self, adapter):
        import json
        result = await adapter.execute({
            "url": "https://app.example.com",
            "check_type": "page_load",
        })
        parsed = json.loads(result.raw_output)
        assert "title" in parsed
