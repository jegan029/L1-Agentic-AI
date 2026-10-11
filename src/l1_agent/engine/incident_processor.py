"""Incident processor: end-to-end orchestration from intake to a terminal outcome.

There is one execution engine (docs/adr/0001-one-execution-engine.md). When a
model is configured it only helps choose between SOP candidates; the SOP that
runs, and every step in it, is executed deterministically by SOPExecutor.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, List, Optional, Set

from src.l1_agent.ai.analyzer import AIAnalyzer
from src.l1_agent.ai.llm_client import LLMClient
from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.connectors.itsm.base import ItsmUnavailable
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.resolution_memory import ResolutionMemory
from src.l1_agent.engine.sop_matcher import MatchResult, SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.engine.state_machine import RunEvent, RunState, outcome_of, transition
from src.l1_agent.escalation.escalator import escalate_to_l2
from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.models.evidence import ExecutionOutcome, ExecutionSummary
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.sop.validator import validate_sop
from src.l1_agent.utils.logging import get_logger, set_correlation_id
from src.l1_agent.utils.metrics import metrics

if TYPE_CHECKING:
    from src.l1_agent.engine.journal import EventPublisher, RunJournal, StoredMatch

logger = get_logger("incident_processor")

MODEL_RATIONALE_LIMIT = 300


class ProcessStatus(Enum):
    PROCESSED = "processed"
    DUPLICATE = "duplicate"


@dataclass
class ProcessResult:
    """What happened to a delivered incident.

    A duplicate delivery creates no run, so it has no summary and no outcome.
    """

    status: ProcessStatus
    incident_number: str
    summary: Optional[ExecutionSummary] = None

    @property
    def duplicate(self) -> bool:
        return self.status == ProcessStatus.DUPLICATE

    @property
    def outcome(self) -> Optional[ExecutionOutcome]:
        return self.summary.outcome if self.summary else None


class IncidentProcessor:
    """Orchestrates the full lifecycle of processing a single incident."""

    def __init__(
        self,
        snow_client: ServiceNowClient,
        sop_matcher: SOPMatcher,
        sop_parser: Optional[SOPParser],  # no longer used: articles are ingested as drafts
        executor: SOPExecutor,
        sop_cache: Optional[List[SOP]] = None,
        confidence_threshold: float = 0.6,
        llm_client: Optional[LLMClient] = None,
        ai_analyzer: Optional[AIAnalyzer] = None,
        memory: Optional[ResolutionMemory] = None,
        event_bus: Optional["EventPublisher"] = None,
        l2_router: Optional[L2Router] = None,
    ) -> None:
        self._snow = snow_client
        self._matcher = sop_matcher
        self._executor = executor
        self._sop_cache = sop_cache or []
        self._threshold = confidence_threshold
        self._processed_ids: Set[str] = set()
        self._rejected_sops: Set[str] = set()
        self._memory = memory
        self._event_bus = event_bus
        self._l2_router = l2_router or L2Router()

        self._llm = llm_client
        self._ai_analyzer = ai_analyzer
        self._ai_enabled = llm_client is not None and ai_analyzer is not None

    @property
    def ai_enabled(self) -> bool:
        return self._ai_enabled

    async def process_incident(self, incident: Incident) -> ProcessResult:
        """Process a single incident end-to-end."""
        set_correlation_id(incident.number)

        # Idempotency check. In-memory until the database arrives (ADR 0004).
        if incident.sys_id in self._processed_ids:
            logger.info("Incident %s already processed; skipping", incident.number)
            metrics.increment("incidents.duplicates")
            return ProcessResult(ProcessStatus.DUPLICATE, incident.number)
        self._processed_ids.add(incident.sys_id)

        metrics.increment("incidents.received")
        logger.info("Processing incident %s", incident.number)

        if self._event_bus:
            await self._event_bus.publish(
                {
                    "type": "incident_received",
                    "incident_number": incident.number,
                    "short_description": incident.short_description,
                    "priority": incident.priority,
                    "category": incident.category,
                }
            )

        summary = await self.run_once(incident)
        return ProcessResult(ProcessStatus.PROCESSED, incident.number, summary)

    async def run_once(
        self,
        incident: Incident,
        journal: Optional["RunJournal"] = None,
        sops: Optional[List[SOP]] = None,
    ) -> ExecutionSummary:
        """Take one incident to a terminal outcome. No duplicate check is made here.

        With a journal the run is durable: called again after a crash, it reuses
        the recorded SOP choice and step results instead of repeating them.

        `sops` is the tenant's published SOPs, read from the version store by the
        service. Without it the processor uses the list it was built with.
        """
        set_correlation_id(incident.number)
        try:
            return await self._run(incident, journal, sops)
        except ItsmUnavailable:
            raise  # nothing was done; the job is tried again
        except Exception as exc:
            # Nothing may leave an incident without an outcome a person can see.
            logger.error("Unhandled error processing %s: %s", incident.number, exc)
            summary = ExecutionSummary(
                incident_number=incident.number,
                sop_id="",
                sop_title="",
                outcome=ExecutionOutcome.NEEDS_HUMAN,
                escalation_reason=f"Internal error: {type(exc).__name__}",
            )
            await self._hand_to_human(incident, summary)
            return summary

    # ── Run ───────────────────────────────────────────────────────────

    async def _claim(self, incident: Incident) -> Optional[ExecutionSummary]:
        """Take the ticket before doing anything. Returns a summary when it is not ours.

        Claiming again for a ticket the agent already holds succeeds, so a
        resumed run passes through here unchanged.
        """
        claim = getattr(self._snow, "claim", None)
        if claim is None:
            return None  # an ITSM stand-in with no notion of ownership
        try:
            result = await claim(incident)
        except Exception as exc:
            metrics.increment("incidents.claim_errors")
            raise ItsmUnavailable(
                f"could not claim {incident.number}: {type(exc).__name__}: {exc}"
            ) from exc
        if result.claimed:
            return None
        metrics.increment("incidents.claim_lost")
        logger.info("Incident %s not claimed: %s", incident.number, result.reason)
        return ExecutionSummary(
            incident_number=incident.number,
            sop_id="",
            sop_title="",
            outcome=outcome_of(transition(RunState.RECEIVED, RunEvent.CLAIM_LOST)),
            escalation_reason=f"Not claimed: {result.reason}",
        )

    async def _run(
        self,
        incident: Incident,
        journal: Optional["RunJournal"] = None,
        sops: Optional[List[SOP]] = None,
    ) -> ExecutionSummary:
        sops = self._sop_cache if sops is None else sops
        # No note, no reassignment: a ticket someone else holds is not touched.
        lost = await self._claim(incident)
        if lost is not None:
            return lost

        state = transition(RunState.RECEIVED, RunEvent.START)
        stored = journal.match if journal else None
        if stored:
            # A resumed run keeps the SOP it started with. Matching again could
            # pick a different one, and model advice is not repeatable.
            match = self._stored_match(sops, stored)
            if match is None:
                summary = ExecutionSummary(
                    incident_number=incident.number,
                    sop_id=stored.sop_id,
                    sop_title="",
                    outcome=ExecutionOutcome.NEEDS_HUMAN,
                    escalation_reason=(
                        f"The run was interrupted and SOP {stored.sop_id} "
                        f"v{stored.sop_version} is no longer available to resume it"
                    ),
                )
                await self._hand_to_human(incident, summary)
                return summary
        else:
            await self._post_note(
                incident,
                "[L1 Agent] Incident received. Searching for applicable SOP...\n"
                f"Category: {incident.category} | CI: {incident.cmdb_ci}",
            )
            match = await self._match_sop(incident, sops)

        if not match.sop:
            state = transition(state, RunEvent.NO_SOP)
            return await self._conclude_unmatched(
                incident,
                state,
                reason_code="no_sop",
                reason="No applicable SOP found",
                note=(
                    "[L1 Agent] ESCALATION: No applicable SOP found.\n"
                    f"Reason: {match.rationale}\n"
                    "Routing to L2 for manual investigation."
                ),
                match=match,
            )

        if match.confidence is None or match.confidence < self._threshold:
            state = transition(state, RunEvent.LOW_CONFIDENCE)
            conf = f"{match.confidence:.2f}" if match.confidence is not None else "N/A"
            return await self._conclude_unmatched(
                incident,
                state,
                reason_code="low_confidence",
                reason=f"Low confidence SOP match: {conf}",
                note=(
                    f"[L1 Agent] ESCALATION: Low confidence SOP match ({conf}).\n"
                    f"Best match: {match.sop.title}\n"
                    f"Rationale: {match.rationale}\n"
                    "Routing to L2 for confirmation."
                ),
                match=match,
            )

        sop = match.sop
        transition(state, RunEvent.SOP_MATCHED)
        if not stored:
            if journal:
                await journal.record_match(sop, match.confidence, match.rationale)
            await self._post_note(
                incident,
                f"[L1 Agent] SOP matched: {sop.title} v{sop.version} "
                f"(confidence: {match.confidence:.2f})\n"
                f"Rationale: {match.rationale}",
            )
            if self._event_bus:
                await self._event_bus.publish(
                    {
                        "type": "sop_matched",
                        "incident_number": incident.number,
                        "sop_id": sop.sop_id,
                        "sop_title": sop.title,
                        "confidence": round(match.confidence, 3),
                    }
                )
            await self._update_state(incident, state=2)  # In Progress

        summary = await self._executor.execute_sop(
            incident,
            sop,
            work_note_callback=self._snow.add_work_note,
            journal=journal,
        )
        return await self._post_conclusion(incident, summary, confidence=match.confidence)

    # ── Matching ──────────────────────────────────────────────────────

    def _executable_sops(self, sops: List[SOP]) -> List[SOP]:
        """Published SOPs that pass validation. Anything else is never matched."""
        usable: List[SOP] = []
        for sop in sops:
            try:
                problems = validate_sop(sop)
            except Exception as exc:  # malformed object: treat as not executable
                problems = [f"could not be validated: {type(exc).__name__}"]
            if not problems:
                usable.append(sop)
            elif sop.sop_id not in self._rejected_sops:
                self._rejected_sops.add(sop.sop_id)
                logger.warning("SOP %s is not executable: %s", sop.sop_id, "; ".join(problems[:5]))
        return usable

    async def _match_sop(self, incident: Incident, sops: List[SOP]) -> MatchResult:
        """Match the incident to one of the published SOPs."""
        candidates = self._executable_sops(sops)
        rule_match = self._matcher.match(incident, candidates)

        if not self._ai_enabled or not candidates:
            return rule_match

        # Model advice. The pick must be one of the candidates and must clear
        # the deterministic threshold on its own score; otherwise it is ignored.
        try:
            advice = await self._ai_analyzer.select_sop(incident, candidates)
        except Exception as exc:
            logger.warning("Model ranking unavailable; using keyword match: %s", type(exc).__name__)
            return rule_match
        picked = advice.sop
        if picked is None or all(picked is not c for c in candidates):
            return rule_match
        own_score = self._matcher.match(incident, [picked])
        if own_score.sop is None or own_score.confidence is None:
            return rule_match
        if own_score.confidence < self._threshold:
            return rule_match
        rationale = (advice.rationale or "")[:MODEL_RATIONALE_LIMIT]
        return MatchResult(
            sop=picked,
            confidence=own_score.confidence,
            rationale=f"{own_score.rationale} | Model-drafted rationale: {rationale}",
        )

    def _stored_match(self, sops: List[SOP], stored: "StoredMatch") -> Optional[MatchResult]:
        """The SOP an earlier attempt chose, if that exact version can still run."""
        for sop in self._executable_sops(sops):
            if sop.sop_id != stored.sop_id or str(sop.version) != stored.sop_version:
                continue
            if stored.sop_hash and sop.content_hash != stored.sop_hash:
                continue  # same number, different content: not the version that started
            return MatchResult(sop=sop, confidence=stored.confidence, rationale=stored.rationale)
        return None

    # ── Conclusions ───────────────────────────────────────────────────

    async def _conclude_unmatched(
        self,
        incident: Incident,
        state: RunState,
        *,
        reason_code: str,
        reason: str,
        note: str,
        match: MatchResult,
    ) -> ExecutionSummary:
        """End a run that never reached execution (no SOP, or low confidence)."""
        await self._post_note(incident, note)
        metrics.increment("incidents.escalated", labels={"reason": reason_code})
        sop = match.sop
        summary = ExecutionSummary(
            incident_number=incident.number,
            sop_id=sop.sop_id if sop else "",
            sop_title=sop.title if sop else "",
            outcome=outcome_of(state),
            escalation_reason=reason,
        )
        await escalate_to_l2(
            incident=incident,
            summary=summary,
            reason=reason_code,
            l2_router=self._l2_router,
            sop_name=sop.title if sop else None,
            confidence=match.confidence if sop else None,
            event_bus=self._event_bus,
            snow_client=self._snow,
        )
        return summary

    async def _post_conclusion(
        self, incident: Incident, summary: ExecutionSummary, confidence: float = 0.0
    ) -> ExecutionSummary:
        """Update the ITSM, post the final work note and record the outcome.

        Returns the summary that was recorded. It differs from the one passed in
        when the ITSM refuses the Resolved update: a resolution the ticket does not
        show is not a resolution, so the run is recorded as NEEDS_HUMAN instead.
        """
        if summary.outcome == ExecutionOutcome.RESOLVED:
            error = await self._resolve_in_itsm(incident, summary)
            if error:
                summary = replace(
                    summary,
                    outcome=ExecutionOutcome.NEEDS_HUMAN,
                    verified_by="",
                    escalation_reason=(
                        f"Verification passed (step {summary.verified_by}) but the ITSM "
                        f"rejected the Resolved update: {error}"
                    ),
                )

        await self._post_note(incident, summary.to_work_note())

        if summary.outcome == ExecutionOutcome.RESOLVED:
            metrics.increment("incidents.resolved")
            if self._memory and summary.sop_id:
                self._memory.record(incident, summary, confidence_used=confidence)
            if self._event_bus:
                await self._event_bus.publish(
                    {
                        "type": "incident_resolved",
                        "incident_number": incident.number,
                        "sop_id": summary.sop_id,
                        "sop_title": summary.sop_title,
                        "verified_by": summary.verified_by,
                        "duration_ms": round(summary.total_duration_ms, 1),
                    }
                )
        elif summary.outcome == ExecutionOutcome.ESCALATED:
            packet = summary.to_escalation_packet()
            await self._post_note(
                incident,
                f"[L1 Agent] Escalation packet:\n{_format_escalation(packet)}",
            )
            metrics.increment("incidents.escalated", labels={"reason": "sop_execution"})
            await escalate_to_l2(
                incident=incident,
                summary=summary,
                reason=summary.escalation_reason or "sop_execution",
                l2_router=self._l2_router,
                sop_name=summary.sop_title or None,
                event_bus=self._event_bus,
                snow_client=self._snow,
            )
        else:
            await self._hand_to_human(incident, summary)
        return summary

    async def hand_to_human(self, incident: Incident, summary: ExecutionSummary) -> None:
        """Route an incident the engine could not finish to a person."""
        await self._hand_to_human(incident, summary)

    async def _hand_to_human(self, incident: Incident, summary: ExecutionSummary) -> None:
        """NEEDS_HUMAN or ABORTED: the incident stays open and is routed to a person."""
        metrics.increment("incidents.needs_human")
        await escalate_to_l2(
            incident=incident,
            summary=summary,
            reason=summary.escalation_reason or summary.outcome_label,
            l2_router=self._l2_router,
            sop_name=summary.sop_title or None,
            event_bus=self._event_bus,
            snow_client=self._snow,
        )

    async def _post_note(self, incident: Incident, note: str) -> None:
        """Post a work note to the incident; swallow errors."""
        try:
            await self._snow.add_work_note(incident.sys_id, note)
        except Exception as exc:
            logger.error("Failed to post work note for %s: %s", incident.number, exc)

    async def _resolve_in_itsm(
        self, incident: Incident, summary: ExecutionSummary
    ) -> Optional[str]:
        """Mark the incident resolved, with the verification as the resolution note.

        Returns None on success, or the reason the ITSM gave for refusing.
        """
        verify = next((r for r in summary.step_results if r.step_id == summary.verified_by), None)
        notes = (
            f"Resolved by L1 Agent using {summary.sop_title} ({summary.sop_id}). "
            f"Verified by step {summary.verified_by}: "
            f"{verify.input_summary if verify else 'verification passed'}."
        )
        try:
            await self._snow.resolve_incident(incident.sys_id, notes)
        except Exception as exc:
            metrics.increment("incidents.resolve_update_failed")
            logger.error("Failed to resolve %s in the ITSM: %s", incident.number, exc)
            return " ".join(str(exc).split())[:300] or type(exc).__name__
        return None

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
