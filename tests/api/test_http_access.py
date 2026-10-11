"""Deny by default on every route, and the webhook signature.

Phase 2 exit criterion 3 is in TestRouteRegistry: a route without an access
declaration stops the application being built.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from src.l1_agent.api.access import (
    AccessKind,
    RouteDeclarationError,
    Routes,
    access_of,
    check_routes,
    public,
    requires,
)
from src.l1_agent.api.security import (
    TIMESTAMP_HEADER,
    SecurityConfigError,
    validate_security_config,
)
from src.l1_agent.auth.permissions import ROLE_PERMISSIONS, Permission, Role
from src.l1_agent.config.settings import (
    AgentSettings,
    AuthSettings,
    ConfigError,
    LLMSettings,
    ServiceNowSettings,
    Settings,
)
from src.l1_agent.db.models import runs
from src.l1_agent.main import L1AgentService, check_servicenow_config
from tests.api.support import (
    PAYLOAD,
    SECRET,
    audit_types,
    build,
    client,
    make_settings,
    session,
    signed_headers,
    token_headers,
)

# Outside demo mode a ServiceNow connection must be fully described. Nothing here calls it.
SNOW = ServiceNowSettings(
    base_url="https://example.service-now.com",
    username="svc",
    password="x",
    assignment_group="L1-Support",
)
PUBLIC_PATHS = {
    "/health",
    "/auth/methods",
    "/auth/login",
    "/auth/callback",
    "/auth/saml/login",
    "/auth/saml/acs",
    "/auth/saml/metadata",
    "/auth/break-glass",
}
READ = (
    Permission.INCIDENTS_READ.value,
    Permission.SYSTEM_READ.value,
    Permission.METRICS_READ.value,
)


NO_SUCH_ID = "00000000-0000-0000-0000-000000000000"


def concrete(path: str) -> str:
    for name, value in (
        ("{incident_number}", "INC1"),
        ("{token_id}", NO_SUCH_ID),
        ("{version_id}", NO_SUCH_ID),
        ("{connector_id}", "splunk"),
        ("{sop_id}", "SOP-NONE"),
    ):
        path = path.replace(name, value)
    return path


async def run_count(ctx) -> int:
    async with ctx.db.tenant(ctx.tenant_id) as conn:
        return (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(runs)
                .where(runs.c.tenant_id == ctx.tenant_id)
            )
        ).scalar()


class TestRouteRegistry:
    """Exit criterion 3."""

    async def test_every_route_of_the_application_is_declared(self, backend):
        app, _ = await build(backend)
        assert len(app.routes) >= 20
        for route in app.routes:
            assert isinstance(route, APIRoute)
            assert access_of(route) is not None, route.path

    async def test_the_public_routes_are_exactly_the_expected_ones(self, backend):
        app, _ = await build(backend)
        found = {r.path for r in app.routes if access_of(r).kind == AccessKind.PUBLIC}
        assert found == PUBLIC_PATHS
        assert [r.path for r in app.routes if access_of(r).kind == AccessKind.WEBHOOK] == [
            "/webhook/incident"
        ]

    def test_route_added_outside_the_registry_stops_start_up(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/api/forgotten")
        async def forgotten() -> dict:
            return {}

        with pytest.raises(RouteDeclarationError, match="/api/forgotten"):
            check_routes(app)

    def test_framework_documentation_routes_stop_start_up(self):
        with pytest.raises(RouteDeclarationError):
            check_routes(FastAPI())  # /docs, /redoc and /openapi.json are undeclared

    def test_mounted_application_stops_start_up(self, tmp_path):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.router.routes.append(Mount("/files", app=StaticFiles(directory=tmp_path)))
        with pytest.raises(RouteDeclarationError, match="/files"):
            check_routes(app)

    def test_registry_refuses_a_route_without_a_declaration(self):
        routes = Routes(FastAPI())
        with pytest.raises(RouteDeclarationError):
            routes.get("/x", None)  # type: ignore[arg-type]
        with pytest.raises(RouteDeclarationError):
            routes.get("/x", "public")  # type: ignore[arg-type]
        with pytest.raises(RouteDeclarationError):
            public("  ")
        with pytest.raises(RouteDeclarationError):
            requires("incidents.read")  # type: ignore[arg-type]

    async def test_route_added_after_start_up_is_refused_at_request_time(self, backend):
        app, ctx = await build(backend)

        async def late() -> dict:
            return {"leak": True}

        app.router.add_api_route("/api/late", late, methods=["GET"])
        async with client(app) as http:
            resp = await http.get("/api/late", headers=await token_headers(ctx, *READ))
        assert resp.status_code == 403
        assert resp.json() == {"error": "forbidden"}


class TestEveryRouteNeedsCredentials:
    async def test_no_route_is_open_except_the_declared_public_ones(self, backend):
        app, _ = await build(backend)
        checked = 0
        async with client(app) as http:
            for route in app.routes:
                for method in route.methods:
                    resp = await http.request(method, concrete(route.path))
                    if route.path in PUBLIC_PATHS:
                        # Reached its handler: not turned away by the access check.
                        assert resp.status_code != 403
                        assert resp.json() != {"error": "unauthorized"}, route.path
                    else:
                        assert resp.status_code == 401, f"{method} {route.path}: {resp.status_code}"
                        assert resp.json() == {"error": "unauthorized"}
                    checked += 1
        assert checked >= 20

    async def test_liveness_reveals_nothing(self, backend):
        app, _ = await build(backend)
        async with client(app) as http:
            assert (await http.get("/health")).json() == {"status": "healthy"}

    @pytest.mark.parametrize(
        "header",
        [
            {},
            {"Authorization": "Bearer wrong" + "x" * 40},
            {"Authorization": "Bearer l1t_" + "0" * 32 + "_" + "x" * 43},
            {"Authorization": "Basic dGVzdDp0ZXN0"},
            {"Authorization": "l1t_abc"},
            {"Authorization": "Bearer "},
        ],
    )
    async def test_bad_tokens_are_refused(self, backend, header):
        app, _ = await build(backend)
        async with client(app) as http:
            for path in ("/api/incidents", "/api/dashboard/summary", "/metrics", "/api/stream"):
                resp = await http.get(path, headers=header)
                assert resp.status_code == 401
                assert resp.json() == {"error": "unauthorized"}

    async def test_a_bad_token_is_not_rescued_by_a_valid_cookie(self, backend):
        app, ctx = await build(backend)
        cookies, _ = await session(ctx, ["operator"])
        async with client(app) as http:
            resp = await http.get(
                "/api/incidents", headers={"Authorization": "Bearer nope"}, cookies=cookies
            )
        assert resp.status_code == 401

    async def test_valid_token_is_accepted_with_security_headers(self, backend):
        app, ctx = await build(backend)
        headers = await token_headers(ctx, *READ)
        async with client(app) as http:
            for path in ("/api/incidents", "/api/dashboard/summary", "/metrics", "/api/status"):
                resp = await http.get(path, headers=headers)
                assert resp.status_code == 200, path
                assert resp.headers["X-Content-Type-Options"] == "nosniff"
                assert resp.headers["X-Frame-Options"] == "DENY"
                assert "max-age" in resp.headers["Strict-Transport-Security"]
                assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
                assert "Access-Control-Allow-Origin" not in resp.headers

    async def test_denied_responses_carry_the_security_headers_too(self, backend):
        app, _ = await build(backend)
        async with client(app) as http:
            resp = await http.get("/api/incidents")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Cache-Control"] == "no-store"

    async def test_cors_is_granted_only_to_a_listed_origin(self, backend):
        app, ctx = await build(backend, agent={"cors_allowed_origins": ("https://ui.example",)})
        headers = await token_headers(ctx, *READ)
        async with client(app) as http:
            listed = await http.get(
                "/api/incidents", headers={**headers, "Origin": "https://ui.example"}
            )
            other = await http.get(
                "/api/incidents", headers={**headers, "Origin": "https://evil.example"}
            )
        assert listed.headers["Access-Control-Allow-Origin"] == "https://ui.example"
        assert "Access-Control-Allow-Origin" not in other.headers

    async def test_sop_editor_and_api_documentation_do_not_exist(self, backend):
        app, ctx = await build(backend)
        headers = await token_headers(ctx, *READ)
        async with client(app) as http:
            for method, path in (
                ("GET", "/sop-editor/"),
                ("POST", "/sop-editor/save"),
                ("POST", "/sop-editor/SOP-MQ-001/delete"),
                ("GET", "/docs"),
                ("GET", "/redoc"),
                ("GET", "/openapi.json"),
            ):
                resp = await http.request(method, path, headers=headers)
                assert resp.status_code in (404, 405), (
                    f"{method} {path} answered {resp.status_code}"
                )


class TestPermissions:
    async def test_token_without_the_permission_is_forbidden_and_audited(self, backend):
        app, ctx = await build(backend)
        headers = await token_headers(ctx, Permission.INCIDENTS_READ.value)
        async with client(app) as http:
            allowed = await http.get("/api/incidents", headers=headers)
            for path in (
                "/api/status",
                "/metrics",
                "/api/audit/events",
                "/api/tokens",
                "/api/role-mappings",
            ):
                resp = await http.get(path, headers=headers)
                assert resp.status_code == 403, path
                assert resp.json() == {"error": "forbidden"}
        assert allowed.status_code == 200
        assert (await audit_types(ctx)).count("access.denied") == 5

    @pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
    async def test_each_role_reaches_exactly_the_routes_its_permissions_allow(self, backend, role):
        app, ctx = await build(backend)
        cookies, _ = await session(ctx, [role.value])
        granted = ROLE_PERMISSIONS[role]
        async with client(app) as http:
            for route in app.routes:
                access = access_of(route)
                if access.kind != AccessKind.PERMISSION or "GET" not in route.methods:
                    continue
                if route.path == "/api/stream":
                    continue  # never ends; its access rule is checked without credentials above
                resp = await http.get(concrete(route.path), cookies=cookies)
                if access.permission in granted:
                    assert resp.status_code in (200, 404), f"{role.value} {route.path}"
                else:
                    assert resp.status_code == 403, f"{role.value} {route.path}"

    def test_role_table_keeps_duties_apart(self):
        admin = ROLE_PERMISSIONS[Role.TENANT_ADMIN]
        assert Permission.AUDIT_READ not in admin
        assert Permission.ACTIONS_APPROVE not in admin and Permission.SOPS_APPROVE not in admin
        assert Permission.SOPS_APPROVE not in ROLE_PERMISSIONS[Role.SOP_AUTHOR]
        assert Permission.SOPS_AUTHOR not in ROLE_PERMISSIONS[Role.SOP_APPROVER]
        assert ROLE_PERMISSIONS[Role.VIEWER] == {Permission.INCIDENTS_READ}
        assert ROLE_PERMISSIONS[Role.AUDITOR] == {Permission.INCIDENTS_READ, Permission.AUDIT_READ}
        assert set(ROLE_PERMISSIONS) == set(Role), "all seven roles are defined"
        assert len(Role) == 7


class TestWebhookSignature:
    async def test_valid_signature_is_accepted_once(self, backend):
        app, ctx = await build(backend)
        body = json.dumps(PAYLOAD).encode()
        headers = signed_headers(body)
        async with client(app) as http:
            first = await http.post("/webhook/incident", content=body, headers=headers)
            replay = await http.post("/webhook/incident", content=body, headers=headers)

        assert first.status_code == 200 and first.json()["status"] == "accepted"
        assert replay.status_code == 401
        assert await run_count(ctx) == 1
        assert "incident.received" in await audit_types(ctx)

    async def test_resent_incident_with_a_fresh_signature_is_a_duplicate(self, backend):
        app, ctx = await build(backend)
        body = json.dumps(PAYLOAD).encode()
        async with client(app) as http:
            await http.post("/webhook/incident", content=body, headers=signed_headers(body))
            again = await http.post(
                "/webhook/incident",
                content=body,
                headers=signed_headers(body, ts=int(time.time()) + 1),
            )
        assert again.status_code == 200 and again.json()["status"] == "duplicate"
        assert await run_count(ctx) == 1

    async def test_bearer_token_is_not_enough_for_the_webhook(self, backend):
        app, ctx = await build(backend)
        async with client(app) as http:
            resp = await http.post(
                "/webhook/incident", json=PAYLOAD, headers=await token_headers(ctx, *READ)
            )
        assert resp.status_code == 401
        assert await run_count(ctx) == 0

    async def test_tampered_body_wrong_secret_and_stale_timestamp(self, backend):
        app, ctx = await build(backend)
        body = json.dumps(PAYLOAD).encode()
        now = int(time.time())
        attempts = [
            (body + b" ", signed_headers(body)),
            (body, signed_headers(body, secret="z" * 40)),
            (body, signed_headers(body, ts=now - 3600)),
            (body, signed_headers(body, ts=now + 3600)),
            (body, {**signed_headers(body), TIMESTAMP_HEADER: "yesterday"}),
        ]
        async with client(app) as http:
            statuses = [
                (await http.post("/webhook/incident", content=b, headers=h)).status_code
                for b, h in attempts
            ]
        assert statuses == [401] * 5
        assert await run_count(ctx) == 0

    @pytest.mark.parametrize(
        "payload",
        [
            {"number": "INC1"},  # no sys_id
            {"sys_id": "abc"},  # no number
            {"sys_id": {"value": "x"}, "number": {"oops": 1}},
            {"sys_id": "a b; rm", "number": "INC1"},
            ["not", "an", "object"],
        ],
    )
    async def test_signed_but_malformed_payload_is_rejected(self, backend, payload):
        app, ctx = await build(backend)
        body = json.dumps(payload).encode()
        async with client(app) as http:
            resp = await http.post("/webhook/incident", content=body, headers=signed_headers(body))
        assert resp.status_code == 400
        assert await run_count(ctx) == 0

    async def test_oversized_body_is_rejected(self, backend):
        app, ctx = await build(backend)
        body = json.dumps({**PAYLOAD, "description": "x" * 400_000}).encode()
        async with client(app) as http:
            declared = await http.post(
                "/webhook/incident", content=body, headers=signed_headers(body)
            )

            async def chunks():
                for i in range(0, len(body), 65536):
                    yield body[i : i + 65536]

            streamed = await http.post(
                "/webhook/incident", content=chunks(), headers=signed_headers(body)
            )
        assert declared.status_code == 413
        assert streamed.status_code == 413, "a body with no declared length is limited too"
        assert await run_count(ctx) == 0


class TestRateLimit:
    async def test_sign_in_routes_have_a_tighter_limit(self, backend):
        app, _ = await build(
            backend, auth={"login_rate_limit_per_minute": 3, "rate_limit_per_minute": 50}
        )
        async with client(app) as http:
            statuses = [(await http.get("/auth/methods")).status_code for _ in range(5)]
            health = await http.get("/health")
        assert statuses == [200, 200, 200, 429, 429]
        assert health.status_code == 200, "liveness is never rate limited"

    async def test_api_limit(self, backend):
        app, ctx = await build(backend, auth={"rate_limit_per_minute": 4})
        headers = await token_headers(ctx, *READ)
        async with client(app) as http:
            statuses = [
                (await http.get("/api/status", headers=headers)).status_code for _ in range(6)
            ]
        assert statuses == [200] * 4 + [429] * 2


class TestStartupConfiguration:
    def test_refuses_to_start_without_a_webhook_secret(self):
        with pytest.raises(SecurityConfigError):
            L1AgentService(replace(make_settings(agent={"webhook_secret": ""}), servicenow=SNOW))

    def test_refuses_short_secrets_and_wildcard_cors(self):
        for bad in (
            make_settings(agent={"webhook_secret": "short"}),
            make_settings(agent={"cors_allowed_origins": ("*",)}),
            make_settings(auth={"audit_checkpoint_key": "short"}),
            make_settings(auth={"session_hours": 0}),
        ):
            with pytest.raises(SecurityConfigError):
                validate_security_config(bad)

    def test_demo_mode_without_credentials_is_loopback_only(self):
        open_demo = Settings(agent=AgentSettings(demo_mode=True))
        validate_security_config(open_demo)  # default host is loopback
        with pytest.raises(SecurityConfigError):
            validate_security_config(
                replace(open_demo, agent=replace(open_demo.agent, webhook_host="0.0.0.0"))
            )

    def test_demo_mode_with_credentials_may_listen_elsewhere(self):
        validate_security_config(
            Settings(
                agent=AgentSettings(demo_mode=True, webhook_host="0.0.0.0", webhook_secret=SECRET),
                auth=AuthSettings(oidc_issuer="https://idp.example"),
            )
        )

    def test_default_bind_address_is_loopback(self):
        assert AgentSettings().webhook_host == "127.0.0.1"

    def test_session_cookie_is_secure_unless_demo_mode(self, monkeypatch):
        monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
        monkeypatch.setenv("AGENT_DEMO_MODE", "false")
        with pytest.raises(ConfigError, match="SESSION_COOKIE_SECURE"):
            Settings.from_env()
        monkeypatch.setenv("AGENT_DEMO_MODE", "true")
        assert Settings.from_env().auth.cookie_secure is False

    async def test_service_outside_demo_mode_needs_a_database(self):
        service = L1AgentService(replace(make_settings(), servicenow=SNOW))
        with pytest.raises(ConfigError, match="DATABASE_URL"):
            await service.setup()

    async def test_demo_service_builds_and_serves_without_credentials(self, monkeypatch):
        service = L1AgentService(
            Settings(agent=AgentSettings(demo_mode=True), llm=LLMSettings(enabled=False))
        )
        try:
            app = await service.create_app()
            async with client(app) as http:
                me = await http.get("/api/me")
                summary = await http.get("/api/dashboard/summary")
                accepted = await http.post("/webhook/incident", json=PAYLOAD)
            assert me.status_code == 200 and me.json()["kind"] == "demo"
            assert summary.status_code == 200
            assert accepted.json()["status"] == "accepted"
        finally:
            await service.close()


class TestServiceNowConfiguration:
    @pytest.mark.parametrize("field", ["base_url", "username", "password", "assignment_group"])
    def test_incomplete_connection_is_refused(self, field):
        with pytest.raises(ConfigError, match="ServiceNow connection needs"):
            check_servicenow_config(replace(SNOW, **{field: ""}))

    def test_unscoped_polling_is_refused(self):
        """Without a group the poller would take every new incident on the instance."""
        with pytest.raises(ConfigError, match="SERVICENOW_ASSIGNMENT_GROUP"):
            L1AgentService(replace(make_settings(), servicenow=replace(SNOW, assignment_group="")))

    def test_plain_http_is_refused(self):
        with pytest.raises(ConfigError, match="https"):
            check_servicenow_config(replace(SNOW, base_url="http://example.service-now.com"))

    def test_demo_mode_uses_the_in_memory_stand_in_by_default(self):
        from src.l1_agent.demo import MockServiceNowClient

        service = L1AgentService(
            Settings(agent=AgentSettings(demo_mode=True), llm=LLMSettings(enabled=False))
        )
        assert isinstance(service._snow_client, MockServiceNowClient)
        assert service._live_servicenow is False

    def test_demo_mode_can_be_pointed_at_a_real_instance(self):
        from src.l1_agent.clients.servicenow_client import ServiceNowClient

        service = L1AgentService(
            Settings(
                agent=AgentSettings(demo_mode=True),
                llm=LLMSettings(enabled=False),
                servicenow=replace(SNOW, live_in_demo=True),
            )
        )
        assert isinstance(service._snow_client, ServiceNowClient)
        assert service._live_servicenow is True
