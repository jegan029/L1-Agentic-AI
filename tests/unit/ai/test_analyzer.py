"""Unit tests for the AI analyzer (SOP selection + incident analysis)."""

from __future__ import annotations

import json

import pytest

from src.l1_agent.ai.analyzer import AIAnalyzer
from src.l1_agent.ai.mock_llm import MockLLMClient, MockLLMResponse
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP


def _make_incident() -> Incident:
    return Incident(
        sys_id="inc-ai-001",
        number="INC900",
        short_description="MQ queue depth high on PAYMENT.REQUEST",
        description="Queue building up since 09:00. Consumer not processing.",
        category="Middleware",
        subcategory="MQ",
        cmdb_ci="PaymentService",
        assignment_group="L1-Middleware-Support",
        priority="2",
    )


def _make_sop() -> SOP:
    return SOP(
        sop_id="SOP-MQ-001",
        title="MQ Queue Depth High - Investigation",
        keywords=["queue depth", "mq", "payment"],
        applicable_services=["PaymentService"],
        applicable_categories=["Middleware"],
        applicable_assignment_groups=["L1-Middleware-Support"],
        tools_required=["IR360", "Splunk"],
    )


def _make_sop_selection_response(sop_id: str, confidence: float) -> MockLLMResponse:
    return MockLLMResponse(
        tool_calls=[
            {
                "id": "call_select_1",
                "function": {
                    "name": "select_sop",
                    "arguments": json.dumps({
                        "sop_id": sop_id,
                        "confidence": confidence,
                        "rationale": "Incident describes MQ queue depth issue matching this SOP.",
                    }),
                },
            }
        ],
    )


class TestAIAnalyzer:
    @pytest.mark.asyncio
    async def test_select_sop_returns_match(self):
        mock_llm = MockLLMClient(
            responses=[_make_sop_selection_response("SOP-MQ-001", 0.92)]
        )
        analyzer = AIAnalyzer(llm_client=mock_llm)
        sop = _make_sop()

        result = await analyzer.select_sop(_make_incident(), [sop])

        assert result.sop is not None
        assert result.sop.sop_id == "SOP-MQ-001"
        assert result.confidence == 0.92
        assert "MQ" in result.rationale

    @pytest.mark.asyncio
    async def test_select_sop_no_sops_available(self):
        mock_llm = MockLLMClient()
        analyzer = AIAnalyzer(llm_client=mock_llm)

        result = await analyzer.select_sop(_make_incident(), [])

        assert result.sop is None
        assert result.confidence == 0.0
        assert "No SOPs available" in result.rationale

    @pytest.mark.asyncio
    async def test_select_sop_escalate_no_match(self):
        mock_llm = MockLLMClient(
            responses=[
                MockLLMResponse(
                    tool_calls=[
                        {
                            "id": "call_esc_1",
                            "function": {
                                "name": "escalate_no_sop",
                                "arguments": json.dumps({
                                    "reason": "No SOP matches a network issue.",
                                }),
                            },
                        }
                    ],
                )
            ]
        )
        analyzer = AIAnalyzer(llm_client=mock_llm)
        sop = _make_sop()

        result = await analyzer.select_sop(_make_incident(), [sop])

        assert result.sop is None
        assert result.confidence == 0.0
        assert "network" in result.rationale.lower() or "No SOP" in result.rationale

    @pytest.mark.asyncio
    async def test_select_sop_unknown_sop_id(self):
        mock_llm = MockLLMClient(
            responses=[_make_sop_selection_response("SOP-UNKNOWN-999", 0.8)]
        )
        analyzer = AIAnalyzer(llm_client=mock_llm)
        sop = _make_sop()

        result = await analyzer.select_sop(_make_incident(), [sop])

        assert result.sop is None
        assert "unknown" in result.rationale.lower()

    @pytest.mark.asyncio
    async def test_analyze_incident_returns_text(self):
        mock_llm = MockLLMClient()
        analyzer = AIAnalyzer(llm_client=mock_llm)

        analysis = await analyzer.analyze_incident(_make_incident())

        assert isinstance(analysis, str)
        assert len(analysis) > 0

    @pytest.mark.asyncio
    async def test_interpret_tool_result(self):
        mock_llm = MockLLMClient()
        analyzer = AIAnalyzer(llm_client=mock_llm)

        interpretation = await analyzer.interpret_tool_result(
            tool_name="mq_check",
            tool_output='{"depth": 1500, "status": "open"}',
            incident_context="MQ queue depth high",
            sop_step_description="Check queue depth",
        )

        assert isinstance(interpretation, str)
        assert len(interpretation) > 0
