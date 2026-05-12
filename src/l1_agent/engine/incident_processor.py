"""Incident processor: end-to-end orchestration from intake to resolution/escalation.

Supports two modes:
1. AI-driven (default when LLM is enabled): The LLM analyzes the ticket,
   selects the SOP, drives tool calls, interprets results, and decides
   whether to resolve or escalate.
2. Rule-based fallback: Uses keyword/regex SOP matching and deterministic
   step execution when the LLM is unavailable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Set

from src.l1_agent.ai.ai_executor import AIExecutor
from src.l1_agent.ai.analyzer import AIAnalyzer, AIMatchResult
from src.l1_agent.ai.llm_client import LLMClient
from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.sop_matcher import MatchResult, SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.models.evidence import ExecutionOutcome, ExecutionSummary
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.engine.resolution_memory import ResolutionMemory
from src.l1_agent.utils.logging import get_logger, set_correlation_id
from src.l1_agent.utils.metrics import metrics

if TYPE_CHECKING:
    from src.l1_agent.clients.servicenow_client import ServiceNowClient as _SNClient
    from src.l1_agent.events.event_bus import EventBus
    from src.l1_agent.store.incident_history import IncidentHistoryStore

from src.l1_agent.escalation.escalator import escalate_to_l2
from src.l1_agent.escalation.l2_router import L2Router

logger = get_logger("incident_processor")


class IncidentProcessor:
    """Orchestrates the full lifecycle of processing a single incident.

    When an LLM client is provided the processor operates in AI mode:
      1. The AI analyzer selects the SOP (with reasoning).
      2. The AI executor drives tool calls via the LLM.
    Otherwise it falls back to the deterministic rule-based engine.
    """

    def __init__(
        self,
        snow_client: ServiceNowClient,
        sop_matcher: SOPMatcher,
        sop_parser: SOPParser,
        executor: SOPExecutor,
        sop_cache: Optional[List[SOP]] = None,
        confidence_threshold: float = 0.6,
        llm_client: Optional[LLMClient] = None,
        ai_analyzer: Optional[AIAnalyzer] = None,
        ai_executor: Optional[AIExecutor] = None,
        memory: Optional[ResolutionMemory] = None,
        event_bus: Optional["EventBus"] = None,
        history_store: Optional["IncidentHistoryStore"] = None,
        l2_router: Optional[L2Router] = None,
    ) -> None:
        self._snow = snow_client
        self._matcher = sop_matcher
        self._parser = sop_parser
        self._executor = executor
        self._sop_cache = sop_cache or []
        self._threshold = confidence_threshold
        self._processed_ids: Set[str] = set()
        self._memory = memory
        self._event_bus = event_bus
        self._history_store = history_store
        self._l2_router = l2_router or L2Router()

        # AI components (optional)
        self._llm = llm_client
        self._ai_analyzer = ai_analyzer
        self._ai_executor = ai_executor
        self._ai_enabled = llm_client is not None

    @property
    def ai_enabled(self) -> bool:
        return self._ai_enabled

    async def process_incident(self, incident: Incident) -> ExecutionSummary:
        """Process a single incident end-to-end."""
        set_correlation_id(incident.number)

        # Idempotency check
        if incident.sys_id in self._processed_ids:
            logger.info("Incident %s already processed; skipping", incident.number)
            return ExecutionSummary(
                incident_number=incident.number,
                sop_id="",
                sop_title="",
                outcome=ExecutionOutcome.RESOLVED,
            )
        self._processed_ids.add(incident.sys_id)

        metrics.increment("incidents.received")
        logger.info("Processing incident %s: %s", incident.number, incident.short_description)

        if self._event_bus:
            await self._event_bus.publish({
                "type": "incident_received",
                "incident_number": incident.number,
                "short_description": incident.short_description,
                "priority": incident.priority,
                "category": incident.category,
            })

        if self._ai_enabled:
            return await self._process_with_ai(incident)
        else:
            return await self._process_rule_based(incident)

    # ── AI-driven processing ──────────────────────────────────────────

    async def _process_with_ai(self, incident: Incident) -> ExecutionSummary:
        """Process incident using AI-driven analysis and execution."""
        logger.info("Using AI-driven processing for %s", incident.number)

        # 1. Post initial work note
        await self._post_note(
            incident,
            "[L1 Agent - AI] Incident received. Starting AI-powered analysis...\n"
            f"Category: {incident.category} | CI: {incident.cmdb_ci}",
        )

        # 2. AI preliminary analysis
        if self._ai_analyzer:
            analysis = await self._ai_analyzer.analyze_incident(incident)
            await self._post_note(
                incident,
                f"[L1 Agent - AI] Preliminary analysis:\n{analysis}",
            )

        # 3. AI-driven SOP selection
        if not self._sop_cache:
            await self._refresh_sop_cache()

        if self._ai_analyzer:
            ai_match = await self._ai_analyzer.select_sop(
                incident, self._sop_cache
            )
        else:
            # Fallback to rule-based matching even in AI mode
            rule_match = self._matcher.match(incident, self._sop_cache)
            ai_match = AIMatchResult(
                sop=rule_match.sop,
                confidence=rule_match.confidence,
                rationale=rule_match.rationale,
            )

        if not ai_match.sop:
            return await self._escalate_no_sop_ai(incident, ai_match)

        if ai_match.confidence is None or ai_match.confidence < self._threshold:
            return await self._escalate_low_confidence_ai(incident, ai_match)

        sop = ai_match.sop
        await self._post_note(
            incident,
            f"[L1 Agent - AI] SOP selected: {sop.title} "
            f"(confidence: {ai_match.confidence:.2f})\n"
            f"AI rationale: {ai_match.rationale}",
        )
        if self._event_bus:
            await self._event_bus.publish({
                "type": "sop_matched",
                "incident_number": incident.number,
                "sop_id": sop.sop_id,
                "sop_title": sop.title,
                "confidence": round(ai_match.confidence, 3),
            })

        # 4. Set incident to In Progress
        await self._update_state(incident, state=2)

        # 5. AI-driven SOP execution
        if self._ai_executor:
            summary = await self._ai_executor.execute_sop(
                incident,
                sop,
                work_note_callback=self._snow.add_work_note,
            )
        else:
            # Fallback to rule-based execution
            summary = await self._executor.execute_sop(
                incident,
                sop,
                work_note_callback=self._snow.add_work_note,
            )

        # 6. Post conclusion
        await self._post_conclusion(incident, summary)
        return summary

    async def _escalate_no_sop_ai(
        self, incident: Incident, match: AIMatchResult
    ) -> ExecutionSummary:
        """Escalate when AI finds no applicable SOP."""
        note = (
            "[L1 Agent - AI] ESCALATION: No applicable SOP found.\n"
            f"AI reasoning: {match.rationale}\n"
            "Routing to L2 for manual investigation."
        )
        await self._post_note(incident, note)
        metrics.increment("incidents.escalated", labels={"reason": "no_sop_ai"})
        summary = ExecutionSummary(
            incident_number=incident.number,
            sop_id="",
            sop_title="",
            outcome=ExecutionOutcome.ESCALATED,
            escalation_reason=f"AI: No applicable SOP. {match.rationale}",
        )
        await escalate_to_l2(
            incident=incident,
            summary=summary,
            reason="no_sop_ai",
            l2_router=self._l2_router,
            event_bus=self._event_bus,
            history_store=self._history_store,
            snow_client=self._snow,
        )
        return summary

    async def _escalate_low_confidence_ai(
        self, incident: Incident, match: AIMatchResult
    ) -> ExecutionSummary:
        """Escalate when AI's SOP confidence is below threshold."""
        sop = match.sop
        sop_title = sop.title if sop else "N/A"
        sop_id = sop.sop_id if sop else ""
        conf_str = f"{match.confidence:.2f}" if match.confidence is not None else "N/A"
        note = (
            f"[L1 Agent - AI] ESCALATION: Low confidence SOP match ({conf_str}).\n"
            f"Best match: {sop_title}\n"
            f"AI rationale: {match.rationale}\n"
            "Routing to L2 for confirmation."
        )
        await self._post_note(incident, note)
        metrics.increment("incidents.escalated", labels={"reason": "low_confidence_ai"})
        summary = ExecutionSummary(
            incident_number=incident.number,
            sop_id=sop_id,
            sop_title=sop_title,
            outcome=ExecutionOutcome.ESCALATED,
            escalation_reason=f"AI: Low confidence ({conf_str}). {match.rationale}",
        )
        await escalate_to_l2(
            incident=incident,
            summary=summary,
            reason="low_confidence_ai",
            l2_router=self._l2_router,
            sop_name=sop_title,
            confidence=match.confidence,
            event_bus=self._event_bus,
            history_store=self._history_store,
            snow_client=self._snow,
        )
        return summary

    # ── Rule-based processing (fallback) ──────────────────────────────

    async def _process_rule_based(self, incident: Incident) -> ExecutionSummary:
        """Process incident using deterministic rule-based engine."""
        logger.info("Using rule-based processing for %s", incident.number)

        # 1. Post initial work note
        await self._post_note(
            incident,
            "[L1 Agent] Incident received. Searching for applicable SOP...\n"
            f"Category: {incident.category} | CI: {incident.cmdb_ci}",
        )

        # 2. Match SOP
        match_result = await self._match_sop(incident)

        if not match_result.sop:
            return await self._escalate_no_sop(incident, match_result)

        if match_result.confidence is None or match_result.confidence < self._threshold:
            return await self._escalate_low_confidence(incident, match_result)

        sop = match_result.sop
        await self._post_note(
            incident,
            f"[L1 Agent] SOP matched: {sop.title} (confidence: {match_result.confidence:.2f})\n"
            f"Rationale: {match_result.rationale}",
        )
        if self._event_bus:
            await self._event_bus.publish({
                "type": "sop_matched",
                "incident_number": incident.number,
                "sop_id": sop.sop_id,
                "sop_title": sop.title,
                "confidence": round(match_result.confidence, 3),
            })

        # 3. Set incident to In Progress
        await self._update_state(incident, state=2)

        # 4. Execute SOP
        summary = await self._executor.execute_sop(
            incident,
            sop,
            work_note_callback=self._snow.add_work_note,
        )

        # 5. Post conclusion
        await self._post_conclusion(incident, summary)

        return summary

    async def _match_sop(self, incident: Incident) -> MatchResult:
        """Match the incident to an SOP from the cache or ServiceNow."""
        if not self._sop_cache:
            await self._refresh_sop_cache()
        return self._matcher.match(incident, self._sop_cache)

    async def _refresh_sop_cache(self) -> None:
        """Reload SOPs from ServiceNow."""
        try:
            records = await self._snow.get_sops()
            self._sop_cache = []
            for record in records:
                sop = self._parser.parse_from_servicenow(record)
                if sop:
                    self._sop_cache.append(sop)
            logger.info("Loaded %d SOPs from ServiceNow", len(self._sop_cache))
        except Exception as exc:
            logger.error("Failed to refresh SOP cache: %s", exc)

    async def _escalate_no_sop(
        self, incident: Incident, match: MatchResult
    ) -> ExecutionSummary:
        """Escalate when no SOP is available."""
        note = (
            "[L1 Agent] ESCALATION: No applicable SOP found.\n"
            f"Reason: {match.rationale}\n"
            "Routing to L2 for manual investigation."
        )
        await self._post_note(incident, note)
        metrics.increment("incidents.escalated", labels={"reason": "no_sop"})
        summary = ExecutionSummary(
            incident_number=incident.number,
            sop_id="",
            sop_title="",
            outcome=ExecutionOutcome.ESCALATED,
            escalation_reason="No applicable SOP found",
        )
        await escalate_to_l2(
            incident=incident,
            summary=summary,
            reason="no_sop",
            l2_router=self._l2_router,
            event_bus=self._event_bus,
            history_store=self._history_store,
            snow_client=self._snow,
        )
        return summary

    async def _escalate_low_confidence(
        self, incident: Incident, match: MatchResult
    ) -> ExecutionSummary:
        """Escalate when SOP confidence is below threshold."""
        sop = match.sop
        sop_title = sop.title if sop else "N/A"
        sop_id = sop.sop_id if sop else ""
        conf_str = f"{match.confidence:.2f}" if match.confidence is not None else "N/A"
        note = (
            f"[L1 Agent] ESCALATION: Low confidence SOP match ({conf_str}).\n"
            f"Best match: {sop_title}\n"
            f"Rationale: {match.rationale}\n"
            "Routing to L2 for confirmation."
        )
        await self._post_note(incident, note)
        metrics.increment("incidents.escalated", labels={"reason": "low_confidence"})
        summary = ExecutionSummary(
            incident_number=incident.number,
            sop_id=sop_id,
            sop_title=sop_title,
            outcome=ExecutionOutcome.ESCALATED,
            escalation_reason=f"Low confidence SOP match: {conf_str}",
        )
        await escalate_to_l2(
            incident=incident,
            summary=summary,
            reason="low_confidence",
            l2_router=self._l2_router,
            sop_name=sop_title,
            confidence=match.confidence,
            event_bus=self._event_bus,
            history_store=self._history_store,
            snow_client=self._snow,
        )
        return summary

    async def _post_conclusion(
        self, incident: Incident, summary: ExecutionSummary, confidence: float = 0.0
    ) -> None:
        """Post the final work note, update incident state, and record outcome."""
        note = summary.to_work_note()
        await self._post_note(incident, note)

        if self._history_store:
            await self._history_store.record(incident, summary)

        if summary.outcome == ExecutionOutcome.RESOLVED:
            await self._update_state(incident, state=6)  # Resolved
            metrics.increment("incidents.resolved")
            if self._memory and summary.sop_id:
                self._memory.record(incident, summary, confidence_used=confidence)
            if self._event_bus:
                await self._event_bus.publish({
                    "type": "incident_resolved",
                    "incident_number": incident.number,
                    "sop_id": summary.sop_id,
                    "sop_title": summary.sop_title,
                    "duration_ms": round(summary.total_duration_ms, 1),
                })
        elif summary.outcome == ExecutionOutcome.ESCALATED:
            escalation_packet = summary.to_escalation_packet()
            await self._post_note(
                incident,
                f"[L1 Agent] Escalation packet:\n{_format_escalation(escalation_packet)}",
            )
            metrics.increment("incidents.escalated", labels={"reason": "sop_execution"})
            reason = summary.escalation_reason or "sop_execution"
            # history already recorded above; pass history_store=None to avoid double-write
            await escalate_to_l2(
                incident=incident,
                summary=summary,
                reason=reason,
                l2_router=self._l2_router,
                sop_name=summary.sop_title or None,
                event_bus=self._event_bus,
                history_store=None,
                snow_client=self._snow,
            )
        else:
            metrics.increment("incidents.failed")
            reason = summary.escalation_reason or "failed"
            await escalate_to_l2(
                incident=incident,
                summary=summary,
                reason=reason,
                l2_router=self._l2_router,
                event_bus=self._event_bus,
                history_store=None,
                snow_client=self._snow,
            )

    async def _post_note(self, incident: Incident, note: str) -> None:
        """Post a work note to the incident; swallow errors."""
        try:
            await self._snow.add_work_note(incident.sys_id, note)
        except Exception as exc:
            logger.error("Failed to post work note for %s: %s", incident.number, exc)

    async def _update_state(self, incident: Incident, state: int) -> None:
        """Update incident state field."""
        try:
            await self._snow.update_incident(incident.sys_id, {"state": str(state)})
        except Exception as exc:
            logger.error("Failed to update state for %s: %s", incident.number, exc)


def _format_escalation(packet: dict) -> str:
    lines = [
        f"Incident: {packet.get('incident_number', '')}",
        f"SOP: {packet.get('sop_title', 'N/A')} ({packet.get('sop_id', '')})",
        f"Reason: {packet.get('escalation_reason', '')}",
        "",
        "Checks performed:",
    ]
    for check in packet.get("checks_performed", []):
        lines.append(
            f"  - {check.get('step_id', '?')} [{check.get('status', '?')}]: "
            f"{check.get('output_summary', '')}"
        )
    lines.append(f"\nRecommended: {packet.get('recommended_actions', '')}")
    return "\n".join(lines)
