"""GET /api/dashboard/summary — KPIs, charts, and 7-day trend."""

from __future__ import annotations

from aiohttp import web

from src.l1_agent.store.incident_history import IncidentHistoryStore
from src.l1_agent.utils.metrics import metrics


def make_dashboard_handler(store: IncidentHistoryStore):
    async def handle_dashboard_summary(request: web.Request) -> web.Response:
        snap = metrics.snapshot()
        counters = snap.get("counters", {})
        histograms = snap.get("histograms", {})
        gauges = snap.get("gauges", {})

        received = int(counters.get("incidents.received", 0))
        resolved = int(counters.get("incidents.resolved", 0))
        failed = int(counters.get("incidents.failed", 0))

        # Escalated: sum all escalation reason labels
        escalated = 0
        for key, val in counters.items():
            if key.startswith("incidents.escalated"):
                escalated += int(val)

        resolution_rate = round(resolved / received * 100, 1) if received else 0.0

        # Average resolution time from histogram
        hist = histograms.get("sop.execution_duration_ms", {})
        avg_ms = hist.get("mean", 0.0)

        currently_processing = int(gauges.get("active_incidents", 0))
        uptime = gauges.get("l1_agent_uptime_seconds", 0.0)

        # Escalation reason breakdown
        escalation_reasons = []
        for key, val in counters.items():
            if key.startswith("incidents.escalated{"):
                # key format: "incidents.escalated{reason=no_sop}"
                reason = key.split("reason=")[-1].rstrip("}")
                escalation_reasons.append({"reason": reason, "count": int(val)})

        return web.json_response({
            "kpi": {
                "incidents_received": received,
                "incidents_resolved": resolved,
                "incidents_escalated": escalated,
                "incidents_failed": failed,
                "resolution_rate_pct": resolution_rate,
                "avg_resolution_ms": round(avg_ms, 1),
                "currently_processing": currently_processing,
            },
            "outcome_breakdown": [
                {"label": "Resolved",  "value": resolved,  "color": "#22c55e"},
                {"label": "Escalated", "value": escalated, "color": "#f97316"},
                {"label": "Failed",    "value": failed,    "color": "#ef4444"},
            ],
            "escalation_reasons": escalation_reasons,
            "trend_7day": store.trend_7day(),
            "uptime_seconds": uptime,
        })

    return handle_dashboard_summary
