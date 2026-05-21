"""CrewIncidentProcessor — drop-in replacement for IncidentProcessor.

When CREWAI_ENABLED=true, L1AgentService instantiates this class instead of
IncidentProcessor. It has the same process_incident() interface but routes
incidents through the four-agent CrewAI pipeline.

Post-conclusion logic (ServiceNow update, history recording, SSE events)
mirrors what IncidentProcessor._post_conclusion() does so the dashboard and
audit trail continue to work unchanged.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.l1_agent.models.evidence import ExecutionOutcome, ExecutionSummary
from src.l1_agent.models.incident import Incident
from src.l1_agent.utils.logging import get_logger

logger = get_logger("crew_processor")


class CrewIncidentProcessor:
    """Processes incidents via the CrewAI multi-agent pipeline."""

    def __init__(
        self,
        *,
        snow_client: Any,
        crewai_settings: Any,
        sops: List[Any],
        adapters: Dict[str, Any],
        event_bus: Optional[Any] = None,
        history_store: Optional[Any] = None,
        l2_router: Optional[Any] = None,
    ) -> None:
        self._snow_client = snow_client
        self._crewai_settings = crewai_settings
        self._sops = sops
        self._adapters = adapters
        self._event_bus = event_bus
        self._history_store = history_store
        self._l2_router = l2_router
        self._processed_ids: set = set()

    async def process_incident(self, incident: Incident) -> ExecutionSummary:
        """Main entry point — mirrors IncidentProcessor.process_incident()."""
        if incident.sys_id and incident.sys_id in self._processed_ids:
            logger.info("Skipping duplicate incident %s", incident.number)
            return ExecutionSummary(
                incident_number=incident.number,
                sop_id="",
                sop_title="",
                outcome=ExecutionOutcome.FAILED,
                escalation_reason="duplicate",
            )
        if incident.sys_id:
            self._processed_ids.add(incident.sys_id)

        logger.info("[CrewAI] Processing incident %s", incident.number)
        await self._publish("incident_received", {
            "type": "incident_received",
            "incident_number": incident.number,
            "short_description": incident.short_description,
            "priority": incident.priority,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        # Run CrewAI synchronously in a thread pool so we don't block the event loop
        from src.l1_agent.crew.crew import run_crew_for_incident

        loop = asyncio.get_event_loop()
        try:
            summary: ExecutionSummary = await loop.run_in_executor(
                None,
                run_crew_for_incident,
                incident,
                self._crewai_settings,
                self._sops,
                self._adapters,
            )
        except Exception as exc:
            logger.error("[CrewAI] Pipeline error for %s: %s", incident.number, exc)
            summary = ExecutionSummary(
                incident_number=incident.number,
                sop_id="CREW-ERROR",
                sop_title="CrewAI Pipeline",
                outcome=ExecutionOutcome.FAILED,
                escalation_reason=str(exc),
            )

        await self._post_conclusion(incident, summary)
        return summary

    # ── Post-conclusion ───────────────────────────────────────────────────────

    async def _post_conclusion(self, incident: Incident, summary: ExecutionSummary) -> None:
        """Mirror IncidentProcessor._post_conclusion(): update ServiceNow, history, events."""
        try:
            work_note = summary.to_work_note()
            await self._snow_client.post_work_note(incident.sys_id, work_note)
        except Exception as exc:
            logger.warning("Failed to post work note for %s: %s", incident.number, exc)

        try:
            if summary.outcome == ExecutionOutcome.RESOLVED:
                await self._snow_client.update_incident(incident.sys_id, {"state": 6})
            elif summary.outcome == ExecutionOutcome.ESCALATED:
                await self._snow_client.update_incident(incident.sys_id, {"state": 2})
        except Exception as exc:
            logger.warning("Failed to update incident state for %s: %s", incident.number, exc)

        if self._history_store is not None:
            try:
                await self._history_store.record(incident, summary)
            except Exception as exc:
                logger.warning("Failed to record history for %s: %s", incident.number, exc)

        if self._l2_router is not None and summary.outcome == ExecutionOutcome.ESCALATED:
            try:
                packet = summary.to_escalation_packet()
                await self._l2_router.route(incident, packet)
            except Exception as exc:
                logger.warning("L2 routing error for %s: %s", incident.number, exc)

        event_type = (
            "incident_resolved"
            if summary.outcome == ExecutionOutcome.RESOLVED
            else "incident_escalated"
        )
        await self._publish(event_type, {
            "type": event_type,
            "incident_number": incident.number,
            "sop_id": summary.sop_id,
            "duration_ms": summary.total_duration_ms,
            "reason": summary.escalation_reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        logger.info(
            "[CrewAI] %s %s in %.0fms (SOP: %s)",
            incident.number,
            summary.outcome.value.upper(),
            summary.total_duration_ms,
            summary.sop_id,
        )

    async def _publish(self, event_type: str, payload: dict) -> None:
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(payload)
            except Exception:
                pass
