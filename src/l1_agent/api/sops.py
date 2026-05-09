"""GET /api/sops/stats — per-SOP aggregated performance stats."""

from __future__ import annotations

from aiohttp import web

from src.l1_agent.store.incident_history import IncidentHistoryStore
from src.l1_agent.store.sop_stats import compute_sop_stats


def make_sops_handler(store: IncidentHistoryStore):
    async def handle_sop_stats(request: web.Request) -> web.Response:
        stats = compute_sop_stats(store.all_records)
        return web.json_response({"items": stats})

    return handle_sop_stats
