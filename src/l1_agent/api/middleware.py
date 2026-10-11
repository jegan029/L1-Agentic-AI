"""ASGI middleware: request size limit, rate limit and security headers."""

from __future__ import annotations

import json
import time
from typing import Callable, Dict, Tuple

from src.l1_agent.api.security import HSTS, SECURITY_HEADERS
from src.l1_agent.utils.metrics import metrics


async def _reject(
    send, status: int, error: str, extra: Tuple[Tuple[bytes, bytes], ...] = ()
) -> None:
    body = json.dumps({"error": error}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers += [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
    headers += list(extra)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Refuse a request body over the limit, whether or not its length is declared."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = True
            if too_large:
                metrics.increment("http.denied", labels={"reason": "too_large"})
                await _reject(send, 413, "payload too large")
                return

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge()
            return message

        async def tracking_send(message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            metrics.increment("http.denied", labels={"reason": "too_large"})
            if not started:
                await _reject(send, 413, "payload too large")


class RateLimitMiddleware:
    """A fixed-window limit per client address, tighter on sign-in routes.

    Counters live in this process. Behind several replicas the effective limit
    is per replica; a shared limit belongs in the ingress.
    """

    def __init__(
        self,
        app,
        per_minute: int,
        login_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self._limits = {"api": per_minute, "login": login_per_minute}
        self._clock = clock
        self._windows: Dict[Tuple[str, str], Tuple[int, int]] = {}

    def _allowed(self, client: str, bucket: str) -> bool:
        window = int(self._clock() // 60)
        key = (client, bucket)
        start, count = self._windows.get(key, (window, 0))
        if start != window:
            start, count = window, 0
        count += 1
        self._windows[key] = (start, count)
        if len(self._windows) > 10_000:  # forget clients from earlier windows
            self._windows = {k: v for k, v in self._windows.items() if v[0] == window}
        return count <= self._limits[bucket]

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return
        client = (scope.get("client") or ("unknown", 0))[0]
        bucket = "login" if scope["path"].startswith("/auth/") else "api"
        if not self._allowed(client, bucket):
            metrics.increment("http.denied", labels={"reason": "rate_limited"})
            await _reject(send, 429, "too many requests", ((b"retry-after", b"60"),))
            return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Add the standard headers to every response that does not set its own."""

    def __init__(self, app, hsts: bool) -> None:
        self.app = app
        self._headers = dict(SECURITY_HEADERS)
        if hsts:
            self._headers["Strict-Transport-Security"] = HSTS

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in self._headers.items():
                    if name.lower().encode() not in present:
                        headers.append((name.lower().encode(), value.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)
