"""Unit tests for the AI-powered SOP executor."""

from __future__ import annotations

import json

import pytest

from src.l1_agent.adapters.mock_adapters import (
    MockAutosysAdapter,
    MockIR360Adapter,
    MockSplunkAdapter,
    MockWindowsShareAdapter,
)
from src.l1_agent.ai.ai_executor import AIExecutor
from src.l1_agent.ai.mock_llm import MockLLMClient, MockLLMResponse
from src.l1_agent.models.evidence import ExecutionOutcome, StepStatus
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, SOPStep


def _make_incident() -> Incident:
    return Incident(
        sys_id="inc-ai-exec-001",
        number="INC800",
        short_description="MQ queue depth high on PAYMENT.REQUEST",
        description="Queue building up. Consumer not processing.",
        category="Middleware",
        subcategory="MQ",
        cmdb_ci="PaymentService",
        assignment_group="L1-Middleware-Support",
        priority="2",
    )


def _make_sop() -> SOP:
    return SOP(
        sop_id="SOP-MQ-001",
        title="MQ Queue Depth High",
        steps=[
            SOPStep(
                step_id="step-1",
                step_type="MQ_CHECK",
                description="Check queue depth",
                parameters={"queue_manager": "QM1", "queue": "Q1", "action": "depth"},
            ),
            SOPStep(
                step_id="step-2",
                step_type="SPLUNK_SEARCH",
                description="Search logs",
                parameters={"query": "error"},
            ),
        ],
        tools_required=["IR360", "Splunk"],
        pre_checks=["Verify MQ connectivity"],
    )


def _make_adapters():
    return {
        "splunk": MockSplunkAdapter(),
        "ir360": MockIR360Adapter(),
        "windows_share": MockWindowsShareAdapter(),
        "autosys": MockAutosysAdapter(),
    }


class TestAIExecutor:
    @pytest.mark.asyncio
    async def test_execute_sop_auto_mode_resolves(self):
        """The mock LLM in auto mode should drive tools and resolve."""
        mock_llm = MockLLMClient()  # auto mode
        executor = AIExecutor(
            llm_client=mock_llm,
            adapters=_make_adapters(),
        )

        summary = await executor.execute_sop(_make_incident(), _make_sop())

        assert summary.outcome == ExecutionOutcome.RESOLVED
        assert len(summary.step_results) > 0
        assert summary.total_duration_ms > 0

    @pytest.mark.asyncio
    async def test_execute_sop_with_scripted_responses(self):
        """Scripted LLM responses should drive the investigation."""
        responses = [
            # First: post work note + call MQ check
            MockLLMResponse(
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {
                            "name": "post_work_note",
                            "arguments": json.dumps({"note": "Starting investigation"}),
                        },
                    },
                    {
                        "id": "call_2",
                        "function": {
                            "name": "mq_check",
                            "arguments": json.dumps({
                                "queue_manager": "QM1",
                                "queue": "Q1",
                                "action": "depth",
                            }),
                        },
                    },
                ],
            ),
            # Second: resolve
            MockLLMResponse(
                tool_calls=[
                    {
                        "id": "call_3",
                        "function": {
                            "name": "resolve_incident",
                            "arguments": json.dumps({
                                "resolution_summary": "Queue depth normal",
                                "evidence_summary": "MQ depth checked OK",
                            }),
                        },
                    },
                ],
            ),
            # Third: empty (end)
            MockLLMResponse(content="Done"),
        ]

        mock_llm = MockLLMClient(responses=responses)
        executor = AIExecutor(
            llm_client=mock_llm,
            adapters=_make_adapters(),
        )

        summary = await executor.execute_sop(_make_incident(), _make_sop())

        assert summary.outcome == ExecutionOutcome.RESOLVED
        step_types = [r.step_type for r in summary.step_results]
        assert "NOTE" in step_types
        assert "MQ_CHECK" in step_types
        assert "RESOLUTION" in step_types

    @pytest.mark.asyncio
    async def test_execute_sop_escalation(self):
        """Scripted LLM escalation should set outcome to ESCALATED."""
        responses = [
            MockLLMResponse(
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {
                            "name": "escalate_to_l2",
                            "arguments": json.dumps({
                                "reason": "Write action required to restart service",
                                "findings_summary": "MQ depth high, consumer crashed",
                                "recommended_actions": "Restart PaymentService consumer",
                            }),
                        },
                    },
                ],
            ),
            MockLLMResponse(content="Escalated"),
        ]

        mock_llm = MockLLMClient(responses=responses)
        executor = AIExecutor(
            llm_client=mock_llm,
            adapters=_make_adapters(),
        )

        summary = await executor.execute_sop(_make_incident(), _make_sop())

        assert summary.outcome == ExecutionOutcome.ESCALATED
        assert "Write action" in summary.escalation_reason or any(
            r.status == StepStatus.ESCALATED for r in summary.step_results
        )

    @pytest.mark.asyncio
    async def test_execute_sop_missing_adapter(self):
        """Tool call for unavailable adapter should record failure."""
        responses = [
            MockLLMResponse(
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {
                            "name": "splunk_search",
                            "arguments": json.dumps({"query": "error"}),
                        },
                    },
                ],
            ),
            MockLLMResponse(
                tool_calls=[
                    {
                        "id": "call_2",
                        "function": {
                            "name": "resolve_incident",
                            "arguments": json.dumps({
                                "resolution_summary": "Could not search Splunk",
                            }),
                        },
                    },
                ],
            ),
            MockLLMResponse(content="Done"),
        ]

        mock_llm = MockLLMClient(responses=responses)
        executor = AIExecutor(
            llm_client=mock_llm,
            adapters={},  # no adapters
        )

        summary = await executor.execute_sop(_make_incident(), _make_sop())

        # Should still complete (LLM decides what to do with errors)
        assert summary.outcome in (
            ExecutionOutcome.RESOLVED,
            ExecutionOutcome.ESCALATED,
            ExecutionOutcome.FAILED,
        )
        failed_steps = [r for r in summary.step_results if r.status == StepStatus.FAIL]
        assert len(failed_steps) >= 1

    @pytest.mark.asyncio
    async def test_work_note_callback_called(self):
        """Work note callback should be invoked for post_work_note tool calls."""
        notes_captured = []

        async def capture_note(sys_id: str, note: str):
            notes_captured.append(note)

        responses = [
            MockLLMResponse(
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {
                            "name": "post_work_note",
                            "arguments": json.dumps({"note": "Test note from AI"}),
                        },
                    },
                ],
            ),
            MockLLMResponse(
                tool_calls=[
                    {
                        "id": "call_2",
                        "function": {
                            "name": "resolve_incident",
                            "arguments": json.dumps({
                                "resolution_summary": "Done",
                            }),
                        },
                    },
                ],
            ),
            MockLLMResponse(content="Done"),
        ]

        mock_llm = MockLLMClient(responses=responses)
        executor = AIExecutor(
            llm_client=mock_llm,
            adapters=_make_adapters(),
        )

        await executor.execute_sop(
            _make_incident(), _make_sop(), work_note_callback=capture_note
        )

        assert len(notes_captured) >= 1
        assert any("Test note from AI" in n for n in notes_captured)

    @pytest.mark.asyncio
    async def test_step_results_have_timing(self):
        """Each step result should have a duration."""
        mock_llm = MockLLMClient()  # auto mode
        executor = AIExecutor(
            llm_client=mock_llm,
            adapters=_make_adapters(),
        )

        summary = await executor.execute_sop(_make_incident(), _make_sop())

        for r in summary.step_results:
            assert r.duration_ms >= 0
