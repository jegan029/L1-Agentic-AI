"""Unit tests for Windows share adapter (validation logic only, no real SMB)."""

from __future__ import annotations

import pytest

from src.l1_agent.adapters.windows_share_adapter import WindowsShareAdapter
from src.l1_agent.config.settings import WindowsShareSettings


class TestWindowsShareAdapter:
    def test_is_allowed_valid_prefix(self):
        settings = WindowsShareSettings(allowed_unc_prefixes=["//fileserver/logs"])
        adapter = WindowsShareAdapter(settings)
        assert adapter._is_allowed("//fileserver/logs/app.log") is True

    def test_is_allowed_invalid_prefix(self):
        settings = WindowsShareSettings(allowed_unc_prefixes=["//fileserver/logs"])
        adapter = WindowsShareAdapter(settings)
        assert adapter._is_allowed("//other-server/data/secret.txt") is False

    def test_is_allowed_empty_list(self):
        settings = WindowsShareSettings(allowed_unc_prefixes=[])
        adapter = WindowsShareAdapter(settings)
        assert adapter._is_allowed("//any/path") is False

    def test_unc_to_os_path(self):
        result = WindowsShareAdapter._unc_to_os_path("//server/share/logs/app.log")
        assert result == "/mnt/server/share/logs/app.log"

    def test_backslash_normalization(self):
        settings = WindowsShareSettings(
            allowed_unc_prefixes=["\\\\fileserver\\logs"]
        )
        adapter = WindowsShareAdapter(settings)
        assert adapter._is_allowed("//fileserver/logs/app.log") is True

    @pytest.mark.asyncio
    async def test_execute_missing_path(self):
        settings = WindowsShareSettings(allowed_unc_prefixes=["//server/logs"])
        adapter = WindowsShareAdapter(settings)
        result = await adapter.execute({"unc_path": ""})
        assert result.success is False
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_execute_disallowed_path(self):
        settings = WindowsShareSettings(allowed_unc_prefixes=["//server/logs"])
        adapter = WindowsShareAdapter(settings)
        result = await adapter.execute({"unc_path": "//evil/path/data.txt"})
        assert result.success is False
        assert "not in the allowed" in result.error

    def test_adapter_name(self):
        adapter = WindowsShareAdapter(WindowsShareSettings())
        assert adapter.adapter_name == "WindowsShare"
