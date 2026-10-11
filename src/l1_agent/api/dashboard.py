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
        needs_human = int(counters.get("incidents.needs_human", 0))

        # Escalated: sum all escalation reason labels
        escalated = 0
        for key, val in counters.items():
            if key.startswith("incidents.escalated"):
                escalated += int(val)

        resolution_rate = round(resolved / received * 100, 1) if received else 0.0

        # Average SOP run time: rule-based and AI executors record separate histograms.
        hist = (
            histograms.get("sop.execution_duration_ms")
            or histograms.get("sop.ai_execution_duration_ms")
            or {}
        )
        # The snapshot's histogram stats expose `avg` (not `mean`). None until a SOP has run.
        avg_ms = hist.get("avg") if hist.get("count") else None

        currently_processing = int(gauges.get("active_incidents", 0))
        # Uptime is a top-level field of the metrics snapshot, not a gauge.
        uptime = snap.get("uptime_seconds", 0.0)

        # Escalation reason breakdown
        escalation_reasons = []
        for key, val in counters.items():
            if key.startswith("incidents.escalated{"):
                # key format: "incidents.escalated{reason=no_sop}"
                reason = key.split("reason=")[-1].rstrip("}")
                escalation_reasons.append({"reason": reason, "count": int(val)})

        return web.json_response(
            {
                "kpi": {
                    "incidents_received": received,
                    "incidents_resolved": resolved,
                    "incidents_escalated": escalated,
                    "incidents_needs_human": needs_human,
                    "resolution_rate_pct": resolution_rate,
                    "avg_resolution_ms": round(avg_ms, 1) if avg_ms is not None else None,
                    "currently_processing": currently_processing,
                },
                "outcome_breakdown": [
                    {"label": "Resolved", "value": resolved, "color": "#22c55e"},
                    {"label": "Escalated", "value": escalated, "color": "#f97316"},
                    {"label": "Needs human", "value": needs_human, "color": "#ef4444"},
                ],
                "escalation_reasons": escalation_reasons,
                "trend_7day": store.trend_7day(),
                "uptime_seconds": uptime,
            }
        )

    return handle_dashboard_summary
