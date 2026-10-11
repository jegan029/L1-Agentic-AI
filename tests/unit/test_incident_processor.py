"""IncidentProcessor: duplicates, model advice limits, fail-closed handling, demo scenarios."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.l1_agent.ai.analyzer import AIAnalyzer, AIMatchResult
from src.l1_agent.demo import (
    DEMO_INCIDENTS,
    MockServiceNowClient,
    build_mock_adapters,
    load_sample_sops,
)
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.incident_processor import IncidentProcessor, ProcessStatus
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.models.evidence import ExecutionOutcome
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP


@pytest.fixture(autouse=True)
def no_email():
    with patch("src.l1_agent.escalation.escalator.send_escalation_email", return_value=False):
        yield


def make_processor(sops, *, analyzer=None, executor=None, threshold=0.6, snow=None):
    llm = MagicMock() if analyzer else None
    return IncidentProcessor(
        snow_client=snow or MockServiceNowClient(),
        sop_matcher=SOPMatcher(confidence_threshold=threshold),
        sop_parser=SOPParser(),
        executor=executor
        or SOPExecutor(adapters=build_mock_adapters(), retry_max_attempts=1, retry_base_delay=0),
        sop_cache=sops,
        confidence_threshold=threshold,
        llm_client=llm,
        ai_analyzer=analyzer,
    )


def incident(n: int = 0) -> Incident:
    return Incident.from_servicenow(DEMO_INCIDENTS[n])


class TestDuplicates:
    @pytest.mark.asyncio
    async def test_duplicate_has_no_summary_and_no_outcome(self):
        snow = MockServiceNowClient()
        processor = make_processor(load_sample_sops(), snow=snow)

        first = await processor.process_incident(incident())
        notes_after_first = len(snow.work_notes)
        second = await processor.process_incident(incident())

        assert first.status == ProcessStatus.PROCESSED and first.summary is not None
        assert second.status == ProcessStatus.DUPLICATE
        assert second.duplicate
        assert second.summary is None
        assert second.outcome is None
        assert len(snow.work_notes) == notes_after_first
        assert not any(u["fields"].get("state") == "6" for u in snow.updates)


class TestDemoScenarios:
    """The demo incidents, through the real processor, with mock adapters."""

    EXPECTED = {
        "INC0012345": (ExecutionOutcome.ESCALATED, "SOP-MQ-001"),  # queue still backed up
        "INC0009005": (ExecutionOutcome.RESOLVED, "SOP-MQ-001"),  # queue drained, verified
        "INC0009003": (ExecutionOutcome.ESCALATED, "SOP-AUTOSYS-001"),  # job still failed
        "INC0009004": (ExecutionOutcome.ESCALATED, "SOP-INFRA-001"),  # memory still at 92%
        "INC0009002": (ExecutionOutcome.ESCALATED, None),  # no SOP clears the threshold
    }

    @pytest.mark.parametrize("payload", DEMO_INCIDENTS, ids=lambda p: p["number"])
    @pytest.mark.asyncio
    async def test_outcome(self, payload):
        snow = MockServiceNowClient()
        processor = make_processor(load_sample_sops(), snow=snow)
        result = await processor.process_incident(Incident.from_servicenow(payload))
        summary = result.summary
        outcome, sop_id = self.EXPECTED[payload["number"]]

        assert summary.outcome == outcome
        resolved_in_itsm = any(u["fields"].get("state") == "6" for u in snow.updates)
        if outcome == ExecutionOutcome.RESOLVED:
            assert summary.verified_by == "step-verify"
            assert resolved_in_itsm
        else:
            assert summary.verified_by == ""
            assert not resolved_in_itsm, "an unresolved incident must not be closed in the ITSM"
            assert any("assignment_group" in u["fields"] for u in snow.updates)
        if sop_id:
            assert summary.sop_id == sop_id

    @pytest.mark.asyncio
    async def test_autosys_job_name_comes_from_the_ticket(self):
        result = await make_processor(load_sample_sops()).process_incident(incident(2))
        inputs = " ".join(r.input_summary for r in result.summary.step_results)
        assert "BATCH_PAYMENT_PROCESS" in inputs
        assert "${job_name}" not in inputs


class TestDraftsAndInvalidSops:
    @pytest.mark.asyncio
    async def test_draft_is_never_matched(self):
        draft = SOP.from_dict(
            json.loads(
                (Path("data/sample_sops") / "mq_queue_depth_high.json").read_text(encoding="utf-8")
            )
        )
        draft.status = "draft"
        result = await make_processor([draft]).process_incident(incident())
        assert result.summary.outcome == ExecutionOutcome.ESCALATED
        assert result.summary.sop_id == ""
        assert result.summary.step_results == []

    @pytest.mark.asyncio
    async def test_prose_articles_from_servicenow_are_not_executable(self):
        snow = MockServiceNowClient()
        snow.get_sops = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "sys_id": "kb1",
                    "short_description": "MQ queue depth high",
                    "text": "<p>Look at the queue and restart the consumer.</p>",
                    "meta": "mq,queue depth,payment",
                    "category": "Middleware",
                }
            ]
        )
        result = await make_processor([], snow=snow).process_incident(incident())
        assert result.summary.outcome == ExecutionOutcome.ESCALATED
        assert result.summary.step_results == []


class TestModelAdvice:
    """The model may only choose among candidates that clear the threshold themselves."""

    def _analyzer(self, sop, confidence=0.99, rationale="trust me"):
        analyzer = MagicMock(spec=AIAnalyzer)
        analyzer.select_sop = AsyncMock(
            return_value=AIMatchResult(sop=sop, confidence=confidence, rationale=rationale)
        )
        return analyzer

    @pytest.mark.asyncio
    async def test_model_cannot_force_a_low_scoring_sop(self):
        sops = load_sample_sops()
        autosys = next(s for s in sops if s.sop_id == "SOP-AUTOSYS-001")
        processor = make_processor(sops, analyzer=self._analyzer(autosys))
        # An MQ incident: the model insists on the Autosys SOP with 0.99 confidence.
        result = await processor.process_incident(incident(0))
        assert result.summary.sop_id == "SOP-MQ-001"

    @pytest.mark.asyncio
    async def test_model_cannot_select_an_sop_outside_the_candidates(self):
        sops = load_sample_sops()
        rogue = SOP.from_dict({**sops[0].to_dict(), "sop_id": "SOP-ROGUE"})
        processor = make_processor(sops, analyzer=self._analyzer(rogue))
        result = await processor.process_incident(incident(0))
        assert result.summary.sop_id == "SOP-MQ-001"

    @pytest.mark.asyncio
    async def test_model_cannot_rescue_an_incident_with_no_matching_sop(self):
        sops = load_sample_sops()
        processor = make_processor(sops, analyzer=self._analyzer(sops[0]))
        result = await processor.process_incident(incident(4))  # SSL certificate
        assert result.summary.outcome == ExecutionOutcome.ESCALATED
        assert result.summary.step_results == []

    @pytest.mark.asyncio
    async def test_model_failure_falls_back_to_keyword_match(self):
        analyzer = MagicMock(spec=AIAnalyzer)
        analyzer.select_sop = AsyncMock(side_effect=RuntimeError("provider down"))
        result = await make_processor(load_sample_sops(), analyzer=analyzer).process_incident(
            incident(0)
        )
        assert result.summary.sop_id == "SOP-MQ-001"

    @pytest.mark.asyncio
    async def test_confidence_is_the_deterministic_score_not_the_models(self):
        sops = load_sample_sops()
        mq = next(s for s in sops if s.sop_id == "SOP-MQ-001")
        snow = MockServiceNowClient()
        processor = make_processor(sops, analyzer=self._analyzer(mq, confidence=0.99), snow=snow)
        await processor.process_incident(incident(0))
        matched = next(n["note"] for n in snow.work_notes if "SOP matched" in n["note"])
        assert "0.99" not in matched
        assert "Model-drafted rationale" in matched


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_engine_exception_becomes_needs_human(self):
        executor = MagicMock()
        executor.execute_sop = AsyncMock(side_effect=RuntimeError("engine bug"))
        snow = MockServiceNowClient()
        processor = make_processor(load_sample_sops(), executor=executor, snow=snow)

        result = await processor.process_incident(incident(0))

        assert result.summary.outcome == ExecutionOutcome.NEEDS_HUMAN
        assert not any(u["fields"].get("state") == "6" for u in snow.updates)
        assert any("assignment_group" in u["fields"] for u in snow.updates)

    @pytest.mark.asyncio
    async def test_missing_adapter_routes_to_a_person_and_leaves_ticket_open(self):
        snow = MockServiceNowClient()
        processor = make_processor(load_sample_sops(), executor=SOPExecutor(adapters={}), snow=snow)
        result = await processor.process_incident(incident(0))
        assert result.summary.outcome == ExecutionOutcome.NEEDS_HUMAN
        assert not any(u["fields"].get("state") == "6" for u in snow.updates)
