"""Interim access control for the HTTP listener.

Deny by default: every route needs a bearer token, except the webhook (HMAC
signature with timestamp and replay protection) and the bare liveness probe.
This is a stopgap until SSO and roles arrive (docs/adr/0006); it exists so
that no build of the service is ever reachable without credentials.

Demo mode may run without credentials, and only on a loopback address.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Callable, Dict, Optional

from aiohttp import web

from src.l1_agent.config.settings import AgentSettings
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("security")

WEBHOOK_PATH = "/webhook/incident"
LIVENESS_PATH = "/health"
TIMESTAMP_HEADER = "X-L1-Timestamp"
SIGNATURE_HEADER = "X-L1-Signature"
MAX_BODY_BYTES = 256 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MIN_SECRET_LENGTH = 32

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class SecurityConfigError(RuntimeError):
    """The service refuses to start with this configuration."""


def validate_security_config(agent: AgentSettings) -> None:
    """Fail start-up when the listener would be reachable without credentials."""
    loopback = agent.webhook_host in LOOPBACK_HOSTS
    for token in agent.api_tokens:
        if len(token) < MIN_SECRET_LENGTH:
            raise SecurityConfigError(
                f"AGENT_API_TOKENS entries must be at least {MIN_SECRET_LENGTH} characters"
            )
    if agent.webhook_secret and len(agent.webhook_secret) < MIN_SECRET_LENGTH:
        raise SecurityConfigError(
            f"AGENT_WEBHOOK_SECRET must be at least {MIN_SECRET_LENGTH} characters"
        )
    if "*" in agent.cors_allowed_origins:
        raise SecurityConfigError("AGENT_CORS_ORIGINS must list origins; '*' is not allowed")

    missing = []
    if not agent.api_tokens:
        missing.append("AGENT_API_TOKENS")
    if not agent.webhook_secret:
        missing.append("AGENT_WEBHOOK_SECRET")
    if not missing:
        return
    if not agent.demo_mode:
        raise SecurityConfigError(
            "Refusing to start without credentials: set " + " and ".join(missing)
        )
    if not loopback:
        raise SecurityConfigError(
            "Demo mode without " + " and ".join(missing) + " may only listen on a loopback "
            "address; set AGENT_WEBHOOK_HOST=127.0.0.1 or configure the credentials"
        )
    logger.warning(
        "DEMO MODE: listener is unauthenticated on loopback (%s unset)", ", ".join(missing)
    )


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    """Signature a sender must put in X-L1-Signature."""
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


class _ReplayCache:
    """Remembers signatures seen inside the accepted time window."""

    def __init__(self, ttl_seconds: int, clock: Callable[[], float]) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._seen: Dict[str, float] = {}

    def check_and_store(self, signature: str) -> bool:
        """Return False if this signature was already used."""
        now = self._clock()
        for key in [k for k, exp in self._seen.items() if exp <= now]:
            del self._seen[key]
        if signature in self._seen:
            return False
        # Twice the skew: a request is valid from ts-skew to ts+skew.
        self._seen[signature] = now + 2 * self._ttl
        return True


def _deny(status: int, reason: str, request: web.Request) -> web.Response:
    metrics.increment("http.denied", labels={"reason": reason})
    logger.warning("Denied %s %s: %s", request.method, request.path, reason)
    # Do not tell the caller which check failed.
    return web.json_response({"error": "unauthorized"}, status=status)


def build_security_middleware(
    agent: AgentSettings,
    clock: Optional[Callable[[], float]] = None,
):
    """Create the aiohttp middleware that authenticates every request."""
    now = clock or time.time
    replay = _ReplayCache(agent.webhook_max_skew_seconds, now)
    api_open = agent.demo_mode and not agent.api_tokens
    webhook_open = agent.demo_mode and not agent.webhook_secret

    def bearer_ok(request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return False
        presented_bytes = presented.strip().encode("utf-8")
        ok = False
        for token in agent.api_tokens:  # no early exit: constant work per request
            ok |= hmac.compare_digest(presented_bytes, token.encode("utf-8"))
        return ok

    async def webhook_ok(request: web.Request) -> Optional[str]:
        """Return None when the signature is valid, otherwise a reason code."""
        timestamp = request.headers.get(TIMESTAMP_HEADER, "")
        signature = request.headers.get(SIGNATURE_HEADER, "")
        if not timestamp or not signature:
            return "missing_signature"
        try:
            sent_at = int(timestamp)
        except ValueError:
            return "bad_timestamp"
        if abs(now() - sent_at) > agent.webhook_max_skew_seconds:
            return "stale_timestamp"
        body = await request.read()
        expected = sign_webhook(agent.webhook_secret, timestamp, body)
        if not hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8", "replace")):
            return "bad_signature"
        if not replay.check_and_store(signature):
            return "replay"
        return None

    @web.middleware
    async def security_middleware(request: web.Request, handler):
        path = request.path
        if request.method == "OPTIONS" or path == LIVENESS_PATH:
            response = await handler(request)
        elif path == WEBHOOK_PATH:
            if not webhook_open:
                reason = await webhook_ok(request)
                if reason:
                    return _deny(401, reason, request)
            response = await handler(request)
        else:
            if not api_open and not bearer_ok(request):
                return _deny(401, "bad_token", request)
            response = await handler(request)
        if not response.prepared:  # streaming responses have already sent headers
            for name, value in SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
        return response

    return security_middleware
