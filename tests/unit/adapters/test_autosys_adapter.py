"""Unit tests for Autosys adapter (non-integration, testing validation logic)."""

from __future__ import annotations

import pytest

from src.l1_agent.adapters.autosys_adapter import AutosysAdapter
from src.l1_agent.config.settings import AutosysSettings


class TestAutosysAdapter:
    def test_validate_job_name_valid(self):
        assert AutosysAdapter._validate_job_name("MY_JOB_01") is True
        assert AutosysAdapter._validate_job_name("batch/payment/step1") is True
        assert AutosysAdapter._validate_job_name("job-name.v2") is True

    def test_validate_job_name_invalid(self):
        assert AutosysAdapter._validate_job_name("job; rm -rf /") is False
        assert AutosysAdapter._validate_job_name("job && malicious") is False
        assert AutosysAdapter._validate_job_name("job$(cmd)") is False
        assert AutosysAdapter._validate_job_name("") is False

    def test_parse_autorep_output(self):
        output = (
            "Job Name           Last Start           Last End             ST/Ex\n"
            "__________________ ____________________ ____________________ _____\n"
            "MY_JOB             04/14/2026 10:00:00  04/14/2026 10:05:00  SU\n"
            "MY_JOB_STEP1       04/14/2026 10:01:00  04/14/2026 10:03:00  SU\n"
            "MY_JOB_STEP2       04/14/2026 10:03:00  04/14/2026 10:05:00  FA\n"
        )
        result = AutosysAdapter._parse_autorep_output(output)
        assert len(result["jobs"]) == 3
        assert result["jobs"][0]["job_name"] == "MY_JOB"
        assert result["jobs"][2]["status"] == "FA"

    def test_parse_empty_output(self):
        result = AutosysAdapter._parse_autorep_output("")
        assert result["jobs"] == []

    @pytest.mark.asyncio
    async def test_execute_missing_job_name(self):
        adapter = AutosysAdapter(AutosysSettings())
        result = await adapter.execute({"job_name": ""})
        assert result.success is False
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_execute_invalid_job_name(self):
        adapter = AutosysAdapter(AutosysSettings())
        result = await adapter.execute({"job_name": "bad; injection"})
        assert result.success is False
        assert "Invalid" in result.error

    def test_adapter_name(self):
        adapter = AutosysAdapter(AutosysSettings())
        assert adapter.adapter_name == "Autosys"
