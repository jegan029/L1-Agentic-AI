"""SSE stream endpoint: broadcasts agent activity events to connected browsers."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from aiohttp import web

from src.l1_agent.events.event_bus import EventBus


async def handle_stream(request: web.Request) -> web.StreamResponse:
    bus: EventBus = request.app["event_bus"]

    resp = web.StreamResponse()
    resp.headers["Content-Type"] = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    await resp.prepare(request)

    q = await bus.subscribe()
    try:
        heartbeat = json.dumps({"type": "heartbeat", "ts": datetime.now(timezone.utc).isoformat()})
        await resp.write(f"data: {heartbeat}\n\n".encode())

        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=25.0)
                await resp.write(f"data: {payload}\n\n".encode())
            except asyncio.TimeoutError:
                ts = datetime.now(timezone.utc).isoformat()
                hb = json.dumps({"type": "heartbeat", "ts": ts})
                await resp.write(f"data: {hb}\n\n".encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        await bus.unsubscribe(q)

    return resp
