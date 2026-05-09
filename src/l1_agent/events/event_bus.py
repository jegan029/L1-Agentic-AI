"""Async SSE event bus: fan-out published events to all connected SSE clients."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import List


class EventBus:
    """Broadcasts events to all subscribed SSE client queues.

    Each connected browser tab gets its own asyncio.Queue. If a queue
    fills up (maxsize=100) the event is silently dropped for that slow
    client — this prevents one lagging browser from blocking the agent.
    """

    def __init__(self) -> None:
        self._subscribers: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: dict) -> None:
        if "ts" not in event:
            event["ts"] = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(event)
        async with self._lock:
            live = []
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                    live.append(q)
                except asyncio.QueueFull:
                    live.append(q)  # keep — just drop this one event
            self._subscribers = live

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


event_bus = EventBus()
