"""GET /api/incidents and GET /api/incidents/{incident_number}."""

from __future__ import annotations

from aiohttp import web

from src.l1_agent.store.incident_history import IncidentHistoryStore


def make_incidents_handler(store: IncidentHistoryStore):
    async def handle_incidents(request: web.Request) -> web.Response:
        incident_number = request.match_info.get("incident_number")

        # Single incident detail
        if incident_number:
            record = store.get_by_number(incident_number)
            if not record:
                return web.json_response({"error": "Not found"}, status=404)
            return web.json_response(record)

        # List with pagination + filters
        try:
            page = max(1, int(request.rel_url.query.get("page", 1)))
            per_page = min(100, max(1, int(request.rel_url.query.get("per_page", 20))))
        except (ValueError, TypeError):
            page, per_page = 1, 20

        search = request.rel_url.query.get("search", "").strip()
        outcome = request.rel_url.query.get("outcome", "").strip()
        priority_raw = request.rel_url.query.get("priority", "").strip()
        priority = int(priority_raw) if priority_raw.isdigit() else None

        total, items = store.query(
            page=page,
            per_page=per_page,
            search=search,
            outcome=outcome,
            priority=priority,
        )

        return web.json_response({
            "total": total,
            "page": page,
            "per_page": per_page,
            "items": items,
        })

    return handle_incidents
