"""Unit tests for confidence threshold enforcement in IncidentProcessor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.l1_agent.models.evidence import ExecutionOutcome, ExecutionSummary
from src.l1_agent.models.incident import Incident


def _make_incident(ci: str = "APP-01") -> Incident:
    return Incident(
        sys_id="SYS001",
        number="INC0000001",
        short_description="Test incident",
        cmdb_ci=ci,
    )


def _make_summary(outcome: ExecutionOutcome = ExecutionOutcome.ESCALATED) -> ExecutionSummary:
    return ExecutionSummary(
        incident_number="INC0000001",
        sop_id="SOP-001",
        sop_title="Test SOP",
        outcome=outcome,
        escalation_reason="test",
    )


@pytest.fixture()
def mock_escalate():
    """Patch escalate_to_l2 so tests don't hit SNOW/SMTP/SSE."""
    with patch(
        "src.l1_agent.engine.incident_processor.escalate_to_l2",
        new_callable=AsyncMock,
    ) as mock:
        yield mock


@pytest.fixture()
def processor(mock_escalate):
    """Build a minimal IncidentProcessor with mocked dependencies."""
    from src.l1_agent.engine.incident_processor import IncidentProcessor

    snow = MagicMock()
    snow.add_work_note = AsyncMock()
    snow.update_incident = AsyncMock()
    snow.get_sops = AsyncMock(return_value=[])

    matcher = MagicMock()
    parser = MagicMock()
    executor = MagicMock()

    return IncidentProcessor(
        snow_client=snow,
        sop_matcher=matcher,
        sop_parser=parser,
        executor=executor,
        confidence_threshold=0.6,
    )


class TestNoneConfidenceEscalates:
    @pytest.mark.asyncio
    async def test_none_confidence_triggers_escalation(self, processor, mock_escalate):
        from src.l1_agent.engine.sop_matcher import MatchResult
        from src.l1_agent.models.sop import SOP

        sop = MagicMock(spec=SOP)
        sop.title = "Test SOP"
        sop.sop_id = "SOP-001"

        # confidence=None should trigger low-confidence escalation
        match = MatchResult(sop=sop, confidence=None, rationale="test")
        processor._sop_cache = [sop]
        processor._matcher.match.return_value = match

        incident = _make_incident()
        summary = await processor._process_rule_based(incident)

        assert summary.outcome == ExecutionOutcome.ESCALATED
        mock_escalate.assert_awaited_once()
        call_kwargs = mock_escalate.call_args.kwargs
        assert call_kwargs["reason"] == "low_confidence"


class TestBelowThresholdEscalates:
    @pytest.mark.asyncio
    async def test_confidence_below_threshold_escalates(self, processor, mock_escalate):
        from src.l1_agent.engine.sop_matcher import MatchResult
        from src.l1_agent.models.sop import SOP

        sop = MagicMock(spec=SOP)
        sop.title = "Test SOP"
        sop.sop_id = "SOP-001"

        match = MatchResult(sop=sop, confidence=0.3, rationale="weak match")
        processor._sop_cache = [sop]
        processor._matcher.match.return_value = match

        incident = _make_incident()
        summary = await processor._process_rule_based(incident)

        assert summary.outcome == ExecutionOutcome.ESCALATED
        mock_escalate.assert_awaited_once()
        call_kwargs = mock_escalate.call_args.kwargs
        assert call_kwargs["reason"] == "low_confidence"
        assert call_kwargs["confidence"] == 0.3

    @pytest.mark.asyncio
    async def test_exactly_at_threshold_does_not_escalate(self, processor, mock_escalate):
        from src.l1_agent.engine.sop_matcher import MatchResult
        from src.l1_agent.models.sop import SOP

        sop = MagicMock(spec=SOP)
        sop.title = "Test SOP"
        sop.sop_id = "SOP-001"
        sop.steps = []

        match = MatchResult(sop=sop, confidence=0.6, rationale="good match")
        processor._sop_cache = [sop]
        processor._matcher.match.return_value = match

        # executor returns a resolved summary
        resolved = _make_summary(ExecutionOutcome.RESOLVED)
        processor._executor.execute_sop = AsyncMock(return_value=resolved)

        incident = _make_incident()
        await processor._process_rule_based(incident)

        # escalate_to_l2 should NOT have been called for the pre-execution check
        for call in mock_escalate.call_args_list:
            assert call.kwargs.get("reason") not in ("low_confidence", "no_sop")


class TestAboveThresholdExecutes:
    @pytest.mark.asyncio
    async def test_confidence_above_threshold_executes_sop(self, processor, mock_escalate):
        from src.l1_agent.engine.sop_matcher import MatchResult
        from src.l1_agent.models.sop import SOP

        sop = MagicMock(spec=SOP)
        sop.title = "MQ SOP"
        sop.sop_id = "SOP-MQ-001"
        sop.steps = []

        match = MatchResult(sop=sop, confidence=0.9, rationale="strong match")
        processor._sop_cache = [sop]
        processor._matcher.match.return_value = match

        resolved = _make_summary(ExecutionOutcome.RESOLVED)
        processor._executor.execute_sop = AsyncMock(return_value=resolved)

        incident = _make_incident()
        summary = await processor._process_rule_based(incident)

        assert summary.outcome == ExecutionOutcome.RESOLVED
        processor._executor.execute_sop.assert_awaited_once()


class TestNoSopEscalates:
    @pytest.mark.asyncio
    async def test_no_sop_found_escalates(self, processor, mock_escalate):
        from src.l1_agent.engine.sop_matcher import MatchResult

        match = MatchResult(sop=None, confidence=0.0, rationale="no match")
        processor._sop_cache = []
        processor._matcher.match.return_value = match

        incident = _make_incident()
        summary = await processor._process_rule_based(incident)

        assert summary.outcome == ExecutionOutcome.ESCALATED
        mock_escalate.assert_awaited_once()
        assert mock_escalate.call_args.kwargs["reason"] == "no_sop"
