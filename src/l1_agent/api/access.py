"""Deny-by-default access control for every route (ADR 0006).

A route exists only if it was added through `Routes` with an access
declaration. `check_routes` runs when the application is built and refuses to
start if any route lacks one. At request time `authorize` runs before every
handler and applies the declaration; a route with none is refused.
"""

from __future__ import annotations

import hmac
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.routing import APIRoute

from src.l1_agent.api.security import (
    CSRF_HEADER,
    SESSION_COOKIE,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign_webhook,
)
from src.l1_agent.audit import log as audit
from src.l1_agent.auth import store
from src.l1_agent.auth.permissions import Permission, Role, permissions_for
from src.l1_agent.auth.store import Principal
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("access")

ACCESS_ATTR = "__l1_access__"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AccessKind(Enum):
    PUBLIC = "public"  # no credentials; must say why
    WEBHOOK = "webhook"  # HMAC signature
    AUTHENTICATED = "authenticated"  # any signed-in principal
    PERMISSION = "permission"  # a principal holding one named permission


@dataclass(frozen=True)
class Access:
    kind: AccessKind
    permission: Optional[Permission] = None
    reason: str = ""


def public(reason: str) -> Access:
    if not reason.strip():
        raise RouteDeclarationError("A public route must say why it is public")
    return Access(AccessKind.PUBLIC, reason=reason)


def webhook() -> Access:
    return Access(AccessKind.WEBHOOK)


def authenticated() -> Access:
    return Access(AccessKind.AUTHENTICATED)


def requires(permission: Permission) -> Access:
    if not isinstance(permission, Permission):
        raise RouteDeclarationError("requires() takes a Permission")
    return Access(AccessKind.PERMISSION, permission=permission)


class RouteDeclarationError(RuntimeError):
    """A route was defined without an access declaration. The service will not start."""


class Routes:
    """The only way to add a route: every one names who may call it."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def _add(self, method: str, path: str, access: Access, **kwargs: Any) -> Callable:
        if not isinstance(access, Access):
            raise RouteDeclarationError(f"{method} {path} has no access declaration")

        def register(endpoint: Callable) -> Callable:
            setattr(endpoint, ACCESS_ATTR, access)
            self._app.add_api_route(path, endpoint, methods=[method], **kwargs)
            return endpoint

        return register

    def get(self, path: str, access: Access, **kwargs: Any) -> Callable:
        return self._add("GET", path, access, **kwargs)

    def post(self, path: str, access: Access, **kwargs: Any) -> Callable:
        return self._add("POST", path, access, **kwargs)

    def put(self, path: str, access: Access, **kwargs: Any) -> Callable:
        return self._add("PUT", path, access, **kwargs)

    def delete(self, path: str, access: Access, **kwargs: Any) -> Callable:
        return self._add("DELETE", path, access, **kwargs)


def access_of(route: Any) -> Optional[Access]:
    found = getattr(getattr(route, "endpoint", None), ACCESS_ATTR, None)
    return found if isinstance(found, Access) else None


def check_routes(app: FastAPI) -> None:
    """Walk the whole route table. Anything undeclared stops start-up."""
    for route in app.routes:
        path = getattr(route, "path", repr(route))
        if not isinstance(route, APIRoute):
            raise RouteDeclarationError(
                f"{path}: only routes added through Routes are allowed "
                f"(found {type(route).__name__})"
            )
        access = access_of(route)
        if access is None:
            raise RouteDeclarationError(f"{path} has no access declaration")
        if access.kind == AccessKind.PERMISSION and access.permission is None:
            raise RouteDeclarationError(f"{path} requires a permission but names none")
        if access.kind == AccessKind.PUBLIC and not access.reason:
            raise RouteDeclarationError(f"{path} is public without a stated reason")


# ── Request-time enforcement ──────────────────────────────────────────


class Denied(Exception):
    def __init__(self, status: int, error: str, reason: str) -> None:
        self.status = status
        self.error = error  # what the caller is told; never which check failed
        self.reason = reason  # for metrics and logs


def demo_principal(tenant_id: uuid.UUID) -> Principal:
    roles = tuple(sorted(r.value for r in Role))
    return Principal(
        tenant_id=tenant_id,
        kind="demo",
        id="demo",
        display="Demo user",
        roles=roles,
        permissions=permissions_for(roles),
    )


async def authenticate(request: Request, ctx: Any) -> Optional[Principal]:
    header = request.headers.get("Authorization")
    if header is not None:
        # A presented token is the only credential considered: no fall back to a cookie.
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented.strip():
            return None
        return await store.load_api_token(ctx.db, presented.strip())
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return await store.load_session(ctx.db, cookie, ctx.settings.auth.session_idle_minutes)
    if ctx.demo_open:
        return demo_principal(ctx.tenant_id)
    return None


async def verify_webhook(request: Request, ctx: Any) -> None:
    if ctx.webhook_open:
        return
    agent = ctx.settings.agent
    timestamp = request.headers.get(TIMESTAMP_HEADER, "")
    signature = request.headers.get(SIGNATURE_HEADER, "")
    if not timestamp or not signature:
        raise Denied(401, "unauthorized", "missing_signature")
    try:
        sent_at = int(timestamp)
    except ValueError:
        raise Denied(401, "unauthorized", "bad_timestamp")
    if abs(time.time() - sent_at) > agent.webhook_max_skew_seconds:
        raise Denied(401, "unauthorized", "stale_timestamp")
    body = await request.body()
    expected = sign_webhook(agent.webhook_secret, timestamp, body)
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8", "replace")):
        raise Denied(401, "unauthorized", "bad_signature")
    # Valid from ts-skew to ts+skew, so remember it for twice the skew.
    fresh = await store.use_webhook_signature(
        ctx.db, ctx.tenant_id, signature, 2 * agent.webhook_max_skew_seconds
    )
    if not fresh:
        raise Denied(401, "unauthorized", "replay")


async def authorize(request: Request) -> None:
    """Runs before every handler. No declaration, no access."""
    ctx = request.app.state.ctx
    access = access_of(request.scope.get("route"))
    if access is None:
        raise Denied(403, "forbidden", "undeclared_route")
    if access.kind == AccessKind.PUBLIC:
        return
    if access.kind == AccessKind.WEBHOOK:
        await verify_webhook(request, ctx)
        return

    principal = await authenticate(request, ctx)
    if principal is None:
        raise Denied(401, "unauthorized", "bad_credentials")
    if access.kind == AccessKind.PERMISSION and not principal.has(access.permission):
        async with ctx.db.tenant(principal.tenant_id) as conn:
            await audit.append(
                conn,
                principal.tenant_id,
                principal.actor,
                "access.denied",
                {
                    "method": request.method,
                    "path": request.scope["route"].path,
                    "permission": access.permission.value,
                },
            )
        raise Denied(403, "forbidden", "missing_permission")
    # A browser sends the session cookie on its own; the token proves the page asked.
    if principal.kind == "user" and request.method not in SAFE_METHODS:
        presented = request.headers.get(CSRF_HEADER, "")
        if not presented or not hmac.compare_digest(presented, principal.csrf_token):
            raise Denied(403, "forbidden", "csrf")
    request.state.principal = principal


def current(request: Request) -> Principal:
    return request.state.principal


def denied_metric(exc: Denied, request: Request) -> None:
    metrics.increment("http.denied", labels={"reason": exc.reason})
    logger.warning("Denied %s %s: %s", request.method, request.url.path, exc.reason)
