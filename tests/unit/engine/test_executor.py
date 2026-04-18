"""Unit tests for SOP execution engine."""

from __future__ import annotations

import pytest

from src.l1_agent.adapters.mock_adapters import (
    MockAutosysAdapter,
    MockIR360Adapter,
    MockSplunkAdapter,
    MockWindowsShareAdapter,
)
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.models.evidence import ExecutionOutcome, StepStatus
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, SOPStep


def _make_incident() -> Incident:
    return Incident(
        sys_id="inc-001",
        number="INC001",
        short_description="Test incident",
    )


def _make_adapters():
    return {
        "splunk": MockSplunkAdapter(),
        "ir360": MockIR360Adapter(),
        "windows_share": MockWindowsShareAdapter(),
        "autosys": MockAutosysAdapter(),
    }


def _make_sop(steps: list) -> SOP:
    return SOP(
        sop_id="SOP-TEST",
        title="Test SOP",
        steps=[SOPStep.from_dict(s) for s in steps],
    )


class TestSOPExecutor:
    @pytest.mark.asyncio
    async def test_execute_note_step(self):
        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "NOTE",
                "description": "A note",
                "parameters": {"text": "Hello from the agent"},
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.outcome == ExecutionOutcome.RESOLVED
        assert len(summary.step_results) == 1
        assert summary.step_results[0].status == StepStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_execute_mq_check(self):
        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "MQ_CHECK",
                "parameters": {"queue_manager": "QM1", "queue": "Q1", "action": "depth"},
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.outcome == ExecutionOutcome.RESOLVED
        assert summary.step_results[0].status == StepStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_execute_splunk_search(self):
        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "SPLUNK_SEARCH",
                "parameters": {"query": "index=main error", "time_range": {"earliest": "-1h"}},
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.outcome == ExecutionOutcome.RESOLVED

    @pytest.mark.asyncio
    async def test_execute_autosys_status(self):
        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "AUTOSYS_STATUS",
                "parameters": {"job_name": "TEST_JOB"},
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.outcome == ExecutionOutcome.RESOLVED

    @pytest.mark.asyncio
    async def test_execute_file_check(self):
        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "FILE_CHECK",
                "parameters": {"unc_path": "//server/logs/app.log"},
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.outcome == ExecutionOutcome.RESOLVED

    @pytest.mark.asyncio
    async def test_execute_decision_any_failed(self):
        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "NOTE",
                "parameters": {"text": "Start"},
                "on_success": "s2",
            },
            {
                "step_id": "s2",
                "step_type": "DECISION",
                "parameters": {"rule": "any_failed"},
                "on_success": "s3",
                "on_failure": "s-esc",
            },
            {
                "step_id": "s3",
                "step_type": "NOTE",
                "parameters": {"text": "All good"},
            },
            {
                "step_id": "s-esc",
                "step_type": "NOTE",
                "parameters": {"text": "Escalating"},
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.outcome == ExecutionOutcome.RESOLVED
        # Should follow success branch since no failures
        step_ids = [r.step_id for r in summary.step_results]
        assert "s3" in step_ids
        assert "s-esc" not in step_ids

    @pytest.mark.asyncio
    async def test_approval_gate_escalates(self):
        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "NOTE",
                "parameters": {"text": "Start"},
                "requires_approval": True,
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.outcome == ExecutionOutcome.ESCALATED
        assert summary.step_results[0].status == StepStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_missing_adapter_escalates(self):
        executor = SOPExecutor(adapters={})  # No adapters
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "SPLUNK_SEARCH",
                "parameters": {"query": "test"},
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.outcome == ExecutionOutcome.ESCALATED

    @pytest.mark.asyncio
    async def test_unknown_step_type_fails(self):
        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "UNKNOWN_TYPE",
                "parameters": {},
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.outcome == ExecutionOutcome.FAILED

    @pytest.mark.asyncio
    async def test_multi_step_chain(self):
        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "NOTE",
                "parameters": {"text": "Start"},
                "on_success": "s2",
            },
            {
                "step_id": "s2",
                "step_type": "MQ_CHECK",
                "parameters": {"queue_manager": "QM1", "queue": "Q1", "action": "depth"},
                "on_success": "s3",
            },
            {
                "step_id": "s3",
                "step_type": "SPLUNK_SEARCH",
                "parameters": {"query": "index=main"},
                "on_success": "s4",
            },
            {
                "step_id": "s4",
                "step_type": "NOTE",
                "parameters": {"text": "Done"},
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.outcome == ExecutionOutcome.RESOLVED
        assert len(summary.step_results) == 4

    @pytest.mark.asyncio
    async def test_work_note_callback_called(self):
        notes = []

        async def capture_note(sys_id: str, note: str):
            notes.append(note)

        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "NOTE",
                "parameters": {"text": "Hello"},
            },
        ])
        await executor.execute_sop(_make_incident(), sop, work_note_callback=capture_note)

        assert len(notes) >= 1

    @pytest.mark.asyncio
    async def test_execution_summary_has_timing(self):
        executor = SOPExecutor(adapters=_make_adapters())
        sop = _make_sop([
            {
                "step_id": "s1",
                "step_type": "NOTE",
                "parameters": {"text": "Timed"},
            },
        ])
        summary = await executor.execute_sop(_make_incident(), sop)

        assert summary.total_duration_ms > 0
        assert summary.step_results[0].duration_ms >= 0
