"""Central escalation handler for L1 Agent.

All escalation paths (pre-execution and post-execution) funnel through
``escalate_to_l2`` so that L2 routing, email notification, SNOW update,
history recording, and SSE event emission happen consistently in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.models.evidence import ExecutionSummary
from src.l1_agent.models.incident import Incident
from src.l1_agent.notifications.email_notifier import send_escalation_email
from src.l1_agent.utils.logging import get_logger

if TYPE_CHECKING:
    from src.l1_agent.clients.servicenow_client import ServiceNowClient
    from src.l1_agent.events.event_bus import EventBus
    from src.l1_agent.store.incident_history import IncidentHistoryStore

logger = get_logger("escalator")


async def escalate_to_l2(
    incident: Incident,
    summary: ExecutionSummary,
    reason: str,
    l2_router: L2Router,
    sop_name: Optional[str] = None,
    confidence: Optional[float] = None,
    event_bus: Optional["EventBus"] = None,
    history_store: Optional["IncidentHistoryStore"] = None,
    snow_client: Optional["ServiceNowClient"] = None,
) -> None:
    """Perform all L2 escalation side-effects in one call.

    1. Resolve the L2 team from the ticket's CI via L2Router.
    2. Update the SNOW ticket's assignment group.
    3. Send an escalation email (best-effort).
    4. Record the incident in the history store (if not already recorded).
    5. Emit an enriched ``incident_escalated`` SSE event.
    """
    l2_team = l2_router.resolve_l2_team(incident.cmdb_ci or "")
    logger.info(
        "Escalating %s to %s (%s) — reason: %s",
        incident.number,
        l2_team["name"],
        l2_team["email"],
        reason,
    )

    # Update SNOW assignment group
    if snow_client:
        try:
            await snow_client.update_incident(
                incident.sys_id,
                {"assignment_group": l2_team["name"]},
            )
        except Exception as exc:
            logger.warning(
                "Failed to update SNOW assignment for %s: %s", incident.number, exc
            )

    # Send email notification (best-effort)
    subject = f"[L1 Agent] Escalation: {incident.number} — {incident.short_description}"
    body = _build_email_body(incident, summary, reason, l2_team, sop_name, confidence)
    email_sent = send_escalation_email(l2_team["email"], subject, body)

    # Record in history store
    if history_store:
        await history_store.record(incident, summary)

    # Emit enriched SSE event
    if event_bus:
        await event_bus.publish({
            "type": "incident_escalated",
            "incident_number": incident.number,
            "reason": reason,
            "l2_team_name": l2_team["name"],
            "l2_team_email": l2_team["email"],
            "email_sent": email_sent,
        })


def _build_email_body(
    incident: Incident,
    summary: ExecutionSummary,
    reason: str,
    l2_team: dict,
    sop_name: Optional[str],
    confidence: Optional[float],
) -> str:
    lines = [
        "L1 Virtual Engineer Agent — Escalation Notification",
        "=" * 52,
        "",
        f"Ticket Number   : {incident.number}",
        f"Short Desc      : {incident.short_description}",
        f"CI              : {incident.cmdb_ci or 'N/A'}",
        f"Category        : {incident.category or 'N/A'}",
        f"Priority        : {incident.priority}",
        "",
        f"Escalation Reason : {reason}",
        f"Predicted SOP     : {sop_name or summary.sop_title or 'N/A'}",
        f"Confidence Score  : {f'{confidence:.2f}' if confidence is not None else 'N/A'}",
        "",
        f"Routed L2 Team  : {l2_team['name']}",
        f"L2 Email        : {l2_team['email']}",
        "",
        "Please investigate and take appropriate action.",
        "",
        "— L1 Virtual Engineer Agent (State Street Operations)",
    ]
    return "\n".join(lines)
