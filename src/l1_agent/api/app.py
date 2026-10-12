"""The HTTP application. Every route is declared through `Routes` with its access rule."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from src.l1_agent.actions import store as action_store
from src.l1_agent.ai import policy as model_policy
from src.l1_agent.ai.prompts import PROMPTS, TASKS
from src.l1_agent.api.access import (
    Denied,
    Routes,
    authenticated,
    authorize,
    check_routes,
    current,
    denied_metric,
    public,
    requires,
    runner,
    webhook,
)
from src.l1_agent.api.middleware import (
    BodyLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from src.l1_agent.api.security import CSRF_HEADER, MAX_BODY_BYTES, SESSION_COOKIE
from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.auth import breakglass, store
from src.l1_agent.auth.oidc import OidcClient, SignInError
from src.l1_agent.auth.permissions import Permission, Role
from src.l1_agent.config.settings import ConfigError, Settings
from src.l1_agent.connectors import hub as connections
from src.l1_agent.db.engine import Database
from src.l1_agent.models.incident import Incident
from src.l1_agent.runner import protocol
from src.l1_agent.runner import store as runner_store
from src.l1_agent.runner.dispatch import hand_out
from src.l1_agent.service import onboarding
from src.l1_agent.service import runs as run_service
from src.l1_agent.service.runs import RunService
from src.l1_agent.sop import ingest
from src.l1_agent.sop import store as sop_store
from src.l1_agent.store.sop_stats import compute_sop_stats
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("api")

_INCIDENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SIGN_IN = Actor.system("sign-in")
WEBHOOK_ACTOR = Actor.system("webhook")
BREAK_GLASS_HOURS = 1
# What a machine token may carry. Tokens never administer access or approve anything.
TOKEN_PERMISSIONS = frozenset(
    {
        Permission.INCIDENTS_READ,
        Permission.SYSTEM_READ,
        Permission.METRICS_READ,
        Permission.AUDIT_READ,
    }
)
STREAM_POLL_SECONDS = 1.0
CLAIM_POLL_SECONDS = 0.25
MAX_REPORTED_CHARS = 8000
STREAM_HEARTBEAT_SECONDS = 25.0


@dataclass
class AppContext:
    settings: Settings
    db: Database
    runs: RunService
    tenant_id: uuid.UUID  # the tenant this deployment signs users into and takes incidents for
    oidc: Optional[OidcClient] = None
    saml: Optional[Any] = None
    demo_open: bool = False
    webhook_open: bool = False
    hub: Optional[Any] = None  # ConnectorHub: this tenant's connectors and knowledge sources
    itsm: Optional[Any] = None  # the ITSM connector, which the ServiceNow KB source reads through
    actions: Optional[Any] = None  # ActionService: the kill switches it sees, for display
    model: Optional[Any] = None  # ModelGateway: the provider and the tenant's model policy
    # RUNNER_MODE=remote: the public keys a runner is configured with ("dispatch", "grant").
    runner_keys: Optional[Dict[str, str]] = None


class TokenRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    permissions: List[str] = Field(min_length=1, max_length=10)
    days: int = Field(default=90, ge=1, le=store.MAX_TOKEN_DAYS)


class RoleMappingRequest(BaseModel):
    group: str = Field(min_length=1, max_length=300)
    role: Role


class BreakGlassRequest(BaseModel):
    password: str = Field(min_length=1, max_length=500)


class SopContentRequest(BaseModel):
    content: Dict[str, Any]


class ApproveRequest(BaseModel):
    # The hash of the content the reviewer looked at. Approval is of that content only.
    content_hash: str = Field(min_length=64, max_length=64)


class ChangesRequest(BaseModel):
    note: str = Field(min_length=1, max_length=sop_store.MAX_NOTE)


class ActionDecisionRequest(BaseModel):
    # The hash of the action the approver was shown. The decision is about that action only.
    action_hash: str = Field(min_length=64, max_length=64)
    note: str = Field(default="", max_length=action_store.MAX_NOTE)


class PolicyRequest(BaseModel):
    autonomy: Optional[str] = Field(default=None, max_length=20)
    rules: Optional[Dict[str, Any]] = None


class ModelPolicyRequest(BaseModel):
    enabled: Optional[bool] = None
    rules: Optional[Dict[str, Any]] = None


class KillSwitchRequest(BaseModel):
    reason: str = Field(default="", max_length=action_store.MAX_NOTE)


class ConnectionRequest(BaseModel):
    config: Dict[str, Any] = Field(default_factory=dict)
    # Slot name to a credential reference (vault:..., file:..., aws:..., env:...). Never a value.
    secrets: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class RunnerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # The runner's public key (Ed25519, base64). The private key never leaves the runner.
    public_key: str = Field(min_length=40, max_length=64)


def summarize(records: List[dict], processing: int, uptime: float) -> Dict[str, Any]:
    """Dashboard numbers for one tenant, from its recorded runs."""
    by_outcome = {"resolved": 0, "escalated": 0, "needs_human": 0}
    skipped = 0  # tickets a person already held; the agent did nothing
    reasons: Dict[str, int] = {}
    durations: List[float] = []
    today = datetime.now(timezone.utc).date()
    trend = {
        (today - timedelta(days=i)).isoformat(): {
            "date": (today - timedelta(days=i)).isoformat(),
            "received": 0,
            "resolved": 0,
            "escalated": 0,
        }
        for i in range(6, -1, -1)
    }
    for r in records:
        outcome = r.get("outcome", "")
        if outcome == "aborted" and str(r.get("escalation_reason", "")).startswith("Not claimed"):
            skipped += 1
            continue
        key = outcome if outcome in ("resolved", "escalated") else "needs_human"
        by_outcome[key] += 1
        if outcome == "escalated":
            code = r.get("reason_code", "sop_execution")
            reasons[code] = reasons.get(code, 0) + 1
        if r.get("sop_id") and r.get("step_count"):
            durations.append(float(r.get("duration_ms", 0.0)))
        day = trend.get(str(r.get("started_at", ""))[:10])
        if day:
            day["received"] += 1
            day["resolved" if outcome == "resolved" else "escalated"] += 1
    received = len(records) - skipped + processing
    resolved = by_outcome["resolved"]
    return {
        "kpi": {
            "incidents_received": received,
            "incidents_resolved": resolved,
            "incidents_escalated": by_outcome["escalated"],
            "incidents_needs_human": by_outcome["needs_human"],
            "incidents_skipped": skipped,
            "resolution_rate_pct": round(resolved / received * 100, 1) if received else 0.0,
            "avg_resolution_ms": round(sum(durations) / len(durations), 1) if durations else None,
            "currently_processing": processing,
        },
        "outcome_breakdown": [
            {"label": "Resolved", "value": resolved, "color": "#22c55e"},
            {"label": "Escalated", "value": by_outcome["escalated"], "color": "#f97316"},
            {"label": "Needs human", "value": by_outcome["needs_human"], "color": "#ef4444"},
        ],
        "escalation_reasons": [{"reason": k, "count": v} for k, v in sorted(reasons.items())],
        "trend_7day": list(trend.values()),
        "uptime_seconds": uptime,
    }


def create_app(ctx: AppContext) -> FastAPI:
    settings = ctx.settings
    app = FastAPI(
        title="L1 and L2 AI-Ops",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # served below, behind a permission
        dependencies=[Depends(authorize)],
    )
    app.state.ctx = ctx
    routes = Routes(app)

    def tenant(request: Request) -> uuid.UUID:
        return current(request).tenant_id

    # ── Liveness and intake ───────────────────────────────────────────

    @routes.get("/health", public("liveness probe; returns no detail"))
    async def health() -> dict:
        return {"status": "healthy"}

    @routes.post("/webhook/incident", webhook())
    async def webhook_incident(request: Request) -> Response:
        try:
            payload = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
        incident = Incident.from_servicenow(payload)
        if not _INCIDENT_ID_RE.match(incident.number) or not _INCIDENT_ID_RE.match(incident.sys_id):
            return JSONResponse({"error": "Missing or invalid number or sys_id"}, status_code=400)
        if hasattr(ctx.itsm, "current") and await ctx.itsm.current() is None:
            # Nothing could be claimed or written back. The sender should try again later.
            return JSONResponse({"error": "No ITSM connection is configured"}, status_code=503)
        result = await ctx.runs.ingest(ctx.tenant_id, incident, WEBHOOK_ACTOR)
        return JSONResponse(
            {
                "status": "accepted" if result.accepted else "duplicate",
                "incident": incident.number,
            }
        )

    # ── Operations ────────────────────────────────────────────────────

    @routes.get("/api/status", requires(Permission.SYSTEM_READ))
    async def status() -> dict:
        return {
            "status": "healthy",
            "demo_mode": settings.agent.demo_mode,
            "metrics": metrics.snapshot(),
        }

    @routes.get("/metrics", requires(Permission.METRICS_READ))
    async def prometheus() -> Response:
        return PlainTextResponse(metrics.prometheus_text())

    @routes.get("/api/dashboard/summary", requires(Permission.INCIDENTS_READ))
    async def dashboard_summary(request: Request) -> dict:
        records = await run_service.recent_records(ctx.db, tenant(request))
        processing = await run_service.open_run_count(ctx.db, tenant(request))
        return summarize(records, processing, metrics.snapshot().get("uptime_seconds", 0.0))

    @routes.get("/api/incidents", requires(Permission.INCIDENTS_READ))
    async def incidents(
        request: Request,
        page: int = 1,
        per_page: int = 20,
        search: str = "",
        outcome: str = "",
        priority: Optional[int] = None,
    ) -> dict:
        page, per_page = max(1, page), min(100, max(1, per_page))
        records = (await run_service.recent_records(ctx.db, tenant(request)))[::-1]
        if search.strip():
            s = search.strip().lower()
            records = [
                r
                for r in records
                if s in r.get("incident_number", "").lower()
                or s in r.get("short_description", "").lower()
                or s in r.get("sop_title", "").lower()
            ]
        if outcome.strip():
            records = [r for r in records if r.get("outcome", "") == outcome.strip().lower()]
        if priority is not None:
            records = [r for r in records if r.get("priority") == priority]
        start = (page - 1) * per_page
        items = [
            {k: v for k, v in r.items() if k != "step_results"}
            for r in records[start : start + per_page]
        ]
        return {"total": len(records), "page": page, "per_page": per_page, "items": items}

    @routes.get("/api/incidents/{incident_number}", requires(Permission.INCIDENTS_READ))
    async def incident_detail(request: Request, incident_number: str) -> Response:
        record = await run_service.record_by_number(ctx.db, tenant(request), incident_number)
        if not record:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse(record)

    @routes.get("/api/sops/stats", requires(Permission.INCIDENTS_READ))
    async def sop_stats(request: Request) -> dict:
        return {
            "items": compute_sop_stats(await run_service.recent_records(ctx.db, tenant(request)))
        }

    @routes.get("/api/stream", requires(Permission.INCIDENTS_READ))
    async def stream(request: Request) -> StreamingResponse:
        tenant_id = tenant(request)
        last = await run_service.latest_event_id(ctx.db, tenant_id)

        def beat() -> str:
            now = datetime.now(timezone.utc).isoformat()
            return f"data: {json.dumps({'type': 'heartbeat', 'ts': now})}\n\n"

        async def feed():
            nonlocal last
            yield beat()
            quiet = 0.0
            while not await request.is_disconnected():
                rows = await run_service.events_after(ctx.db, tenant_id, last)
                for event_id, payload in rows:
                    last = event_id
                    yield f"data: {json.dumps(payload)}\n\n"
                quiet = 0.0 if rows else quiet + STREAM_POLL_SECONDS
                if quiet >= STREAM_HEARTBEAT_SECONDS:
                    quiet = 0.0
                    yield beat()
                await asyncio.sleep(STREAM_POLL_SECONDS)

        return StreamingResponse(
            feed(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Who am I, and sign-in ─────────────────────────────────────────

    @routes.get("/api/me", authenticated())
    async def me(request: Request) -> dict:
        principal = current(request)
        return {
            "kind": principal.kind,
            "id": principal.id,
            "display": principal.display,
            "roles": list(principal.roles),
            "permissions": sorted(p.value for p in principal.permissions),
            "csrf_token": principal.csrf_token,
            "csrf_header": CSRF_HEADER,
        }

    @routes.get(
        "/auth/methods", public("the sign-in page needs to know which sign-in methods exist")
    )
    async def auth_methods() -> dict:
        return {
            "oidc": ctx.oidc is not None,
            "saml": ctx.saml is not None,
            "break_glass": bool(settings.auth.break_glass_password_hash),
            "demo": ctx.demo_open,
        }

    async def refuse_sign_in(method: str, reason: str) -> Response:
        metrics.increment("auth.login_failed", labels={"method": method})
        async with ctx.db.tenant(ctx.tenant_id) as conn:
            await audit.append(
                conn,
                ctx.tenant_id,
                SIGN_IN,
                "auth.login_failed",
                {"method": method, "reason": reason},
            )
        # The browser is not told why.
        return JSONResponse({"error": "sign-in failed"}, status_code=401)

    async def establish_session(
        method: str,
        issuer: str,
        subject: str,
        email: str,
        name: str,
        groups: List[str],
        *,
        roles: Optional[List[str]] = None,
        hours: Optional[int] = None,
    ) -> Response:
        async with ctx.db.tenant(ctx.tenant_id) as conn:
            granted = roles or await store.roles_for_groups(conn, ctx.tenant_id, groups)
        if not granted:
            return await refuse_sign_in(method, f"no role is mapped to any group of {subject}")
        async with ctx.db.tenant(ctx.tenant_id) as conn:
            user_id = await store.upsert_user(conn, ctx.tenant_id, issuer, subject, email, name)
            cookie, session_id = await store.create_session(
                conn, ctx.tenant_id, user_id, granted, method, hours or settings.auth.session_hours
            )
            await audit.append(
                conn,
                ctx.tenant_id,
                Actor("user", f"{issuer}|{subject}"),
                "auth.login",
                {"method": method, "roles": granted, "session_id": str(session_id)},
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            max_age=(hours or settings.auth.session_hours) * 3600,
            httponly=True,
            secure=settings.auth.cookie_secure,
            samesite="lax",
            path="/",
        )
        return response

    @routes.get("/auth/login", public("starts sign-in; redirects to the identity provider"))
    async def oidc_login() -> Response:
        if ctx.oidc is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        state, nonce, verifier = await store.create_login_state(ctx.db, ctx.tenant_id)
        return RedirectResponse(
            await ctx.oidc.authorization_url(state, nonce, verifier), status_code=302
        )

    @routes.get("/auth/callback", public("the identity provider redirects the browser here"))
    async def oidc_callback(code: str = "", state: str = "") -> Response:
        if ctx.oidc is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        pending = await store.consume_login_state(ctx.db, ctx.tenant_id, state) if state else None
        if pending is None or not code:
            return await refuse_sign_in("oidc", "unknown, expired or reused state")
        nonce, verifier = pending
        try:
            claims = await ctx.oidc.exchange(code, verifier, nonce)
        except SignInError as exc:
            return await refuse_sign_in("oidc", str(exc))
        except Exception as exc:  # provider unreachable, malformed answers
            return await refuse_sign_in("oidc", f"provider error: {type(exc).__name__}")
        return await establish_session(
            "oidc",
            ctx.oidc.issuer,
            str(claims["sub"]),
            str(claims.get("email", "")),
            str(claims.get("name", "")),
            ctx.oidc.groups(claims),
        )

    @routes.get(
        "/auth/saml/login", public("starts SAML sign-in; redirects to the identity provider")
    )
    async def saml_login() -> Response:
        if ctx.saml is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        state, _, _ = await store.create_login_state(ctx.db, ctx.tenant_id)
        url, request_id = ctx.saml.login_redirect(state)
        await store.bind_login_state(ctx.db, ctx.tenant_id, state, request_id)
        return RedirectResponse(url, status_code=302)

    @routes.post("/auth/saml/acs", public("the identity provider posts the signed assertion here"))
    async def saml_acs(request: Request) -> Response:
        if ctx.saml is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        relay_state = (form.get("RelayState") or [""])[0]
        saml_response = (form.get("SAMLResponse") or [""])[0]
        pending = (
            await store.consume_login_state(ctx.db, ctx.tenant_id, relay_state)
            if relay_state
            else None
        )
        if pending is None or not saml_response:
            return await refuse_sign_in("saml", "no sign-in was started for this response")
        request_id, _ = pending
        try:
            identity = await asyncio.to_thread(ctx.saml.process, saml_response, request_id)
        except SignInError as exc:
            return await refuse_sign_in("saml", str(exc))
        return await establish_session(
            "saml",
            ctx.saml.idp_entity_id,
            identity["subject"],
            identity["email"],
            identity["name"],
            identity["groups"],
        )

    @routes.get(
        "/auth/saml/metadata", public("service provider metadata for the identity provider")
    )
    async def saml_metadata() -> Response:
        if ctx.saml is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return Response(ctx.saml.metadata(), media_type="application/xml")

    @routes.post("/auth/break-glass", public("emergency sign-in; disabled unless a hash is set"))
    async def break_glass(body: BreakGlassRequest) -> Response:
        stored = settings.auth.break_glass_password_hash
        if not stored:
            return JSONResponse({"error": "Not found"}, status_code=404)
        ok = await asyncio.to_thread(breakglass.verify_password, body.password, stored)
        if not ok:
            return await refuse_sign_in("break_glass", "wrong password")
        async with ctx.db.tenant(ctx.tenant_id) as conn:
            await audit.append(
                conn, ctx.tenant_id, Actor("user", "local|break-glass"), "auth.break_glass_used", {}
            )
        logger.warning("Break-glass account used")
        return await establish_session(
            "break_glass",
            breakglass.ISSUER,
            breakglass.SUBJECT,
            "",
            "Break-glass administrator",
            [],
            roles=[Role.TENANT_ADMIN.value],
            hours=BREAK_GLASS_HOURS,
        )

    @routes.post("/auth/logout", authenticated())
    async def logout(request: Request) -> Response:
        await store.revoke_session(ctx.db, current(request))
        response = JSONResponse({"status": "signed out"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # ── Access administration ─────────────────────────────────────────

    @routes.get("/api/tokens", requires(Permission.TOKENS_MANAGE))
    async def list_tokens(request: Request) -> dict:
        return {"items": await store.list_api_tokens(ctx.db, tenant(request))}

    @routes.post("/api/tokens", requires(Permission.TOKENS_MANAGE), status_code=201)
    async def create_token(request: Request, body: TokenRequest) -> Response:
        allowed = {p.value for p in TOKEN_PERMISSIONS}
        if not set(body.permissions) <= allowed:
            return JSONResponse(
                {"error": "A token may only carry: " + ", ".join(sorted(allowed))}, status_code=400
            )
        principal = current(request)
        token, token_id = await store.create_api_token(
            ctx.db, principal.tenant_id, body.name, body.permissions, body.days, principal.actor
        )
        # Shown once. Only its hash is stored.
        return JSONResponse({"id": str(token_id), "token": token}, status_code=201)

    @routes.delete("/api/tokens/{token_id}", requires(Permission.TOKENS_MANAGE))
    async def revoke_token(request: Request, token_id: uuid.UUID) -> Response:
        principal = current(request)
        if not await store.revoke_api_token(ctx.db, principal.tenant_id, token_id, principal.actor):
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse({"status": "revoked"})

    @routes.get("/api/role-mappings", requires(Permission.ROLES_MANAGE))
    async def role_mappings(request: Request) -> dict:
        return {"items": await store.list_role_mappings(ctx.db, tenant(request))}

    @routes.put("/api/role-mappings", requires(Permission.ROLES_MANAGE))
    async def grant_role(request: Request, body: RoleMappingRequest) -> dict:
        principal = current(request)
        changed = await store.set_role_mapping(
            ctx.db, principal.tenant_id, body.group, body.role.value, principal.actor, grant=True
        )
        return {"status": "granted" if changed else "unchanged"}

    @routes.delete("/api/role-mappings", requires(Permission.ROLES_MANAGE))
    async def remove_role(request: Request, group: str, role: Role) -> Response:
        principal = current(request)
        changed = await store.set_role_mapping(
            ctx.db, principal.tenant_id, group, role.value, principal.actor, grant=False
        )
        if not changed:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse({"status": "removed"})

    # ── SOPs: library, authoring and review ───────────────────────────

    @routes.get("/api/sops", requires(Permission.INCIDENTS_READ))
    async def list_sops(request: Request, status: str = "", sop_id: str = "") -> dict:
        items = await sop_store.list_versions(
            ctx.db, tenant(request), sop_key=sop_id[:100], status=status[:20]
        )
        return {"items": items}

    @routes.get("/api/sop-versions/{version_id}", requires(Permission.INCIDENTS_READ))
    async def sop_version(request: Request, version_id: uuid.UUID) -> dict:
        return await sop_store.review(ctx.db, tenant(request), version_id)

    @routes.post("/api/sops", requires(Permission.SOPS_AUTHOR), status_code=201)
    async def create_sop_draft(request: Request, body: SopContentRequest) -> Response:
        principal = current(request)
        version_id = await sop_store.create_draft(
            ctx.db, principal.tenant_id, principal.actor, body.content
        )
        return JSONResponse({"id": str(version_id)}, status_code=201)

    @routes.put("/api/sop-versions/{version_id}", requires(Permission.SOPS_AUTHOR))
    async def update_sop_draft(
        request: Request, version_id: uuid.UUID, body: SopContentRequest
    ) -> dict:
        principal = current(request)
        await sop_store.update_draft(
            ctx.db, principal.tenant_id, principal.actor, version_id, body.content
        )
        return {"status": "saved"}

    @routes.delete("/api/sop-versions/{version_id}", requires(Permission.SOPS_AUTHOR))
    async def discard_sop_draft(request: Request, version_id: uuid.UUID) -> dict:
        principal = current(request)
        await sop_store.discard(ctx.db, principal.tenant_id, principal.actor, version_id)
        return {"status": "discarded"}

    @routes.post("/api/sop-versions/{version_id}/submit", requires(Permission.SOPS_AUTHOR))
    async def submit_sop(request: Request, version_id: uuid.UUID) -> dict:
        principal = current(request)
        digest = await sop_store.submit(ctx.db, principal.tenant_id, principal.actor, version_id)
        return {"status": "in_review", "content_hash": digest}

    @routes.post("/api/sop-versions/{version_id}/approve", requires(Permission.SOPS_APPROVE))
    async def approve_sop(request: Request, version_id: uuid.UUID, body: ApproveRequest) -> dict:
        principal = current(request)
        await sop_store.approve(
            ctx.db, principal.tenant_id, principal.actor, version_id, body.content_hash
        )
        return {"status": "approved"}

    @routes.post(
        "/api/sop-versions/{version_id}/request-changes", requires(Permission.SOPS_APPROVE)
    )
    async def request_sop_changes(
        request: Request, version_id: uuid.UUID, body: ChangesRequest
    ) -> dict:
        principal = current(request)
        await sop_store.request_changes(
            ctx.db, principal.tenant_id, principal.actor, version_id, body.note
        )
        return {"status": "draft"}

    @routes.post("/api/sop-versions/{version_id}/publish", requires(Permission.SOPS_APPROVE))
    async def publish_sop(request: Request, version_id: uuid.UUID) -> dict:
        principal = current(request)
        number = await sop_store.publish(ctx.db, principal.tenant_id, principal.actor, version_id)
        return {"status": "published", "version": number}

    @routes.post("/api/sops/{sop_id}/retire", requires(Permission.SOPS_APPROVE))
    async def retire_sop(request: Request, sop_id: str) -> dict:
        principal = current(request)
        await sop_store.retire(ctx.db, principal.tenant_id, principal.actor, sop_id[:100])
        return {"status": "retired"}

    @routes.post("/api/knowledge/sync", requires(Permission.SOPS_AUTHOR))
    async def knowledge_sync(request: Request) -> dict:
        principal = current(request)
        if ctx.hub is None:
            return {"items": []}
        sources = await ctx.hub.knowledge_sources(principal.tenant_id, ctx.itsm)
        results = await ingest.sync_all(
            ctx.db,
            principal.tenant_id,
            sources,
            principal.actor,
            drafter=ingest.model_drafter(ctx.model, principal.tenant_id, ctx.hub.registry),
            registry=ctx.hub.registry,
        )
        return {"items": [r.to_dict() for r in results]}

    # ── Connectors ────────────────────────────────────────────────────

    @routes.get("/api/connectors", requires(Permission.SYSTEM_READ))
    async def connectors(request: Request) -> dict:
        if ctx.hub is None:
            return {"items": []}
        items = await ctx.hub.describe(tenant(request))
        if ctx.itsm is not None and not hasattr(ctx.itsm, "current"):
            # Demo mode: tickets go to the built-in stand-in, whatever is stored.
            for item in items:
                if item["family"] == "itsm":
                    item.update(configured=True, enabled=True, origin="demo")
        return {"items": items}

    @routes.put("/api/connectors/{connector_id}/connection", requires(Permission.CONNECTORS_MANAGE))
    async def set_connection(
        request: Request, connector_id: str, body: ConnectionRequest
    ) -> Response:
        principal = current(request)
        try:
            await connections.set_connection(
                ctx.db,
                principal.tenant_id,
                principal.actor,
                connector_id[:64],
                body.config,
                body.secrets,
                enabled=body.enabled,
            )
        except ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if ctx.hub is not None:
            ctx.hub.forget(principal.tenant_id)
        if hasattr(ctx.itsm, "current"):
            await ctx.itsm.current(fresh=True)  # an ITSM connection is used from now on
        return JSONResponse({"status": "saved"})

    @routes.delete(
        "/api/connectors/{connector_id}/connection", requires(Permission.CONNECTORS_MANAGE)
    )
    async def remove_connection(request: Request, connector_id: str) -> Response:
        principal = current(request)
        removed = await connections.remove_connection(
            ctx.db, principal.tenant_id, principal.actor, connector_id[:64]
        )
        if not removed:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if ctx.hub is not None:
            ctx.hub.forget(principal.tenant_id)
        if hasattr(ctx.itsm, "current"):
            await ctx.itsm.current(fresh=True)
        return JSONResponse({"status": "removed"})

    @routes.post("/api/connectors/{connector_id}/test", requires(Permission.CONNECTORS_MANAGE))
    async def test_connector(request: Request, connector_id: str) -> Response:
        principal = current(request)
        if ctx.hub is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        try:
            checks = await ctx.hub.test(principal.tenant_id, connector_id[:64], ctx.itsm)
        except LookupError:
            return JSONResponse({"error": "Not found"}, status_code=404)
        async with ctx.db.tenant(principal.tenant_id) as conn:
            await audit.append(
                conn,
                principal.tenant_id,
                principal.actor,
                "connection.tested",
                {"connector": connector_id[:64], "ok": all(c.ok for c in checks)},
            )
        return JSONResponse(
            {
                "ok": all(c.ok for c in checks),
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
            }
        )

    # ── The runner (ADR 0010) ─────────────────────────────────────────

    async def signed_body(request: Request) -> Optional[dict]:
        try:
            body = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    @routes.post(protocol.CLAIM_PATH, runner())
    async def runner_claim(request: Request) -> Response:
        """A runner asks for calls. Held open, for a bounded time, while there are none."""
        who: runner_store.RunnerIdentity = request.state.runner
        body = await signed_body(request)
        if body is None:
            return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
        try:
            limit = min(protocol.MAX_CLAIM, max(1, int(body.get("max", 1))))
            wait = min(protocol.MAX_WAIT_SECONDS, max(0.0, float(body.get("wait_seconds", 0))))
        except (TypeError, ValueError):
            return JSONResponse({"error": "max and wait_seconds are numbers"}, status_code=400)
        reported = body.get("runner")
        if not isinstance(reported, dict) or len(protocol.canonical(reported)) > MAX_REPORTED_CHARS:
            reported = {}
        await runner_store.seen(ctx.db, who, reported)
        loop = asyncio.get_running_loop()
        ends = loop.time() + wait
        while True:
            calls = await hand_out(ctx.db, who, limit)
            if calls or loop.time() >= ends or await request.is_disconnected():
                return JSONResponse({"calls": calls})
            await asyncio.sleep(CLAIM_POLL_SECONDS)

    @routes.post("/runner/v1/calls/{call_id}/result", runner())
    async def runner_result(request: Request, call_id: uuid.UUID) -> Response:
        who: runner_store.RunnerIdentity = request.state.runner
        body = await signed_body(request)
        outcome = body.get("outcome") if body else None
        result = body.get("result") if body else None
        if outcome not in protocol.OUTCOMES or not isinstance(result, dict):
            return JSONResponse({"error": "Expected an outcome and a result"}, status_code=400)
        accepted = await runner_store.record_result(
            ctx.db, who.tenant_id, call_id, outcome, result, runner_id=who.id
        )
        if not accepted:
            # Not this runner's call, already answered, or withdrawn.
            return JSONResponse({"error": "The result was not accepted"}, status_code=409)
        return JSONResponse({"status": "recorded"})

    @routes.get("/api/runners", requires(Permission.SYSTEM_READ))
    async def list_runners(request: Request) -> dict:
        remote = bool(ctx.hub is not None and ctx.hub.remote)
        return {
            "mode": "remote" if remote else "embedded",
            # Public keys: what a runner is configured with to check calls and grants.
            "keys": dict(ctx.runner_keys or {}),
            "items": await runner_store.list_runners(ctx.db, tenant(request)),
        }

    @routes.post("/api/runners", requires(Permission.CONNECTORS_MANAGE), status_code=201)
    async def enrol_runner(request: Request, body: RunnerRequest) -> Response:
        principal = current(request)
        runner_id = await runner_store.enrol(
            ctx.db, principal.tenant_id, principal.actor, body.name, body.public_key
        )
        return JSONResponse(
            {
                "id": str(runner_id),
                "identity": protocol.identity(principal.tenant_id, runner_id),
            },
            status_code=201,
        )

    @routes.delete("/api/runners/{runner_id}", requires(Permission.CONNECTORS_MANAGE))
    async def revoke_runner(request: Request, runner_id: uuid.UUID) -> Response:
        principal = current(request)
        if not await runner_store.revoke(ctx.db, principal.tenant_id, principal.actor, runner_id):
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse({"status": "revoked"})

    # ── Set-up ────────────────────────────────────────────────────────

    @routes.get("/api/onboarding", requires(Permission.SYSTEM_READ))
    async def onboarding_status(request: Request) -> dict:
        return await onboarding.status(ctx, tenant(request))

    # ── Actions, approvals and policy (ADR 0008) ──────────────────────

    @routes.get("/api/actions", requires(Permission.INCIDENTS_READ))
    async def list_actions(
        request: Request, status: str = "", run_id: str = "", limit: int = 100
    ) -> dict:
        items = await action_store.list_actions(
            ctx.db, tenant(request), status=status[:20], run_id=run_id[:40], limit=limit
        )
        return {"items": items}

    @routes.get("/api/actions/{action_id}", requires(Permission.INCIDENTS_READ))
    async def action_detail(request: Request, action_id: uuid.UUID) -> dict:
        return action_store.describe(await action_store.get(ctx.db, tenant(request), action_id))

    @routes.post("/api/actions/{action_id}/approve", requires(Permission.ACTIONS_APPROVE))
    async def approve_action(
        request: Request, action_id: uuid.UUID, body: ActionDecisionRequest
    ) -> dict:
        principal = current(request)
        return await action_store.decide(
            ctx.db,
            principal.tenant_id,
            principal.actor,
            action_id,
            approve=True,
            seen_hash=body.action_hash,
            note_text=body.note,
        )

    @routes.post("/api/actions/{action_id}/reject", requires(Permission.ACTIONS_APPROVE))
    async def reject_action(
        request: Request, action_id: uuid.UUID, body: ActionDecisionRequest
    ) -> dict:
        principal = current(request)
        return await action_store.decide(
            ctx.db,
            principal.tenant_id,
            principal.actor,
            action_id,
            approve=False,
            seen_hash=body.action_hash,
            note_text=body.note,
        )

    async def policy_view(tenant_id: uuid.UUID) -> dict:
        policy = await action_store.get_policy(ctx.db, tenant_id)
        ceiling = settings.actions
        local = bool(ctx.actions and ctx.actions._local_switch())
        return {
            **policy.to_dict(),
            "global_kill_switch": await action_store.global_kill_switch(ctx.db),
            "local_kill_switch": local,
            "ceiling": {"max_tier": ceiling.max_tier, "max_autonomy": ceiling.max_autonomy},
        }

    @routes.get("/api/policy", requires(Permission.SYSTEM_READ))
    async def get_policy(request: Request) -> dict:
        return await policy_view(tenant(request))

    @routes.put("/api/policy", requires(Permission.POLICY_MANAGE))
    async def set_policy(request: Request, body: PolicyRequest) -> dict:
        principal = current(request)
        await action_store.set_policy(
            ctx.db, principal.tenant_id, principal.actor, autonomy=body.autonomy, rules=body.rules
        )
        return await policy_view(principal.tenant_id)

    @routes.post("/api/kill-switch/engage", requires(Permission.ACTIONS_STOP))
    async def engage_kill_switch(request: Request, body: KillSwitchRequest) -> dict:
        principal = current(request)
        await action_store.set_kill_switch(
            ctx.db, principal.tenant_id, principal.actor, True, body.reason
        )
        logger.warning("Kill switch engaged for tenant %s", principal.tenant_id)
        return {"kill_switch": True}

    @routes.post("/api/kill-switch/release", requires(Permission.POLICY_MANAGE))
    async def release_kill_switch(request: Request, body: KillSwitchRequest) -> dict:
        principal = current(request)
        await action_store.set_kill_switch(
            ctx.db, principal.tenant_id, principal.actor, False, body.reason
        )
        return {"kill_switch": False}

    # ── Model use (ADR 0009) ──────────────────────────────────────────

    async def model_view(tenant_id: uuid.UUID) -> dict:
        policy = await model_policy.get_model_policy(ctx.db, tenant_id)
        provider = ctx.model.describe() if ctx.model else {"configured": False}
        reason = None
        if ctx.model and ctx.model.provider is not None:
            # Why each task would be refused right now, or null when it would be sent.
            reason = {
                task: model_policy.permits(policy, ctx.model.provider, task) for task in TASKS
            }
        return {
            **policy.to_dict(),
            "provider": provider,
            "refused": reason,
            "prompts": {p.task: p.version for p in PROMPTS.values()},
            "usage_today": await model_policy.usage(ctx.db, tenant_id),
        }

    @routes.get("/api/model", requires(Permission.SYSTEM_READ))
    async def get_model(request: Request) -> dict:
        return await model_view(tenant(request))

    @routes.put("/api/model", requires(Permission.POLICY_MANAGE))
    async def set_model(request: Request, body: ModelPolicyRequest) -> dict:
        principal = current(request)
        await model_policy.set_model_policy(
            ctx.db, principal.tenant_id, principal.actor, enabled=body.enabled, rules=body.rules
        )
        return await model_view(principal.tenant_id)

    @routes.get("/api/model/calls", requires(Permission.AUDIT_READ))
    async def model_calls(request: Request, limit: int = 100) -> dict:
        return {"items": await model_policy.recent_calls(ctx.db, tenant(request), limit)}

    # ── Audit ─────────────────────────────────────────────────────────

    @routes.get("/api/audit/events", requires(Permission.AUDIT_READ))
    async def audit_events(
        request: Request, after: int = 0, limit: int = 100, run_id: str = ""
    ) -> dict:
        items = await audit.events_for(
            ctx.db,
            tenant(request),
            correlation_id=run_id,
            after_seq=max(0, after),
            limit=min(500, max(1, limit)),
        )
        return {"items": items}

    @routes.get("/api/audit/verify", requires(Permission.AUDIT_READ))
    async def audit_verify(request: Request) -> dict:
        result = await audit.verify(ctx.db, tenant(request))
        return {
            "ok": result.ok,
            "events_checked": result.events_checked,
            "head_seq": result.head_seq,
            "head_hash": result.head_hash,
            "problems": result.problems[:50],
        }

    # ── Assembly ──────────────────────────────────────────────────────

    @routes.get("/api/openapi.json", requires(Permission.SYSTEM_READ))
    async def openapi() -> dict:
        return app.openapi()

    @app.exception_handler(sop_store.SopError)
    async def on_sop_error(request: Request, exc: sop_store.SopError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "problems": exc.problems}, status_code=exc.status)

    @app.exception_handler(model_policy.ModelPolicyError)
    async def on_model_policy_error(
        request: Request, exc: model_policy.ModelPolicyError
    ) -> JSONResponse:
        return JSONResponse({"error": str(exc), "problems": exc.problems[:50]}, status_code=400)

    @app.exception_handler(action_store.ActionError)
    async def on_action_error(request: Request, exc: action_store.ActionError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "problems": exc.problems}, status_code=exc.status)

    @app.exception_handler(runner_store.RunnerError)
    async def on_runner_error(request: Request, exc: runner_store.RunnerError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(Denied)
    async def on_denied(request: Request, exc: Denied) -> JSONResponse:
        denied_metric(exc, request)
        return JSONResponse({"error": exc.error}, status_code=exc.status)

    if settings.agent.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.agent.cors_allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", CSRF_HEADER],
        )
    app.add_middleware(
        RateLimitMiddleware,
        per_minute=settings.auth.rate_limit_per_minute,
        login_per_minute=settings.auth.login_rate_limit_per_minute,
    )
    app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.auth.cookie_secure)

    check_routes(app)
    return app
