"""What the API returns: runs, dashboard numbers, the audit log, the event stream,
and that none of it crosses a tenant boundary (exit criterion 2, at the HTTP layer)."""

from __future__ import annotations

import json

import pytest

from src.l1_agent.api import app as app_module
from src.l1_agent.api.access import AccessKind, access_of
from src.l1_agent.audit.log import Actor
from src.l1_agent.auth import store
from src.l1_agent.connectors import hub as connections
from src.l1_agent.demo import DEMO_INCIDENTS
from src.l1_agent.models.incident import Incident
from src.l1_agent.runner import store as runner_store
from src.l1_agent.sop import store as sop_store
from tests.api.support import (
    ADMIN,
    build,
    client,
    seed_action,
    seed_model,
    seed_runner,
    session,
    token_headers,
)

INTAKE = Actor.system("test-intake")
ALL_READ = ("incidents.read", "system.read", "metrics.read", "audit.read")


async def run_incidents(ctx, tenant_id, indexes) -> list:
    numbers = []
    for i in indexes:
        incident = Incident.from_servicenow(DEMO_INCIDENTS[i])
        await ctx.runs.ingest(tenant_id, incident, INTAKE)
        await ctx.runs.work_once([tenant_id])
        numbers.append(incident.number)
    return numbers


class TestRunsApi:
    async def test_dashboard_incidents_and_sop_stats(self, backend):
        app, ctx = await build(backend)
        numbers = await run_incidents(ctx, ctx.tenant_id, range(5))
        headers = await token_headers(ctx, *ALL_READ)
        async with client(app) as http:
            summary = (await http.get("/api/dashboard/summary", headers=headers)).json()
            listing = (await http.get("/api/incidents", headers=headers)).json()
            resolved = (await http.get("/api/incidents?outcome=resolved", headers=headers)).json()
            search = (await http.get("/api/incidents?search=memory", headers=headers)).json()
            page = (await http.get("/api/incidents?per_page=2&page=2", headers=headers)).json()
            detail = (await http.get(f"/api/incidents/{numbers[1]}", headers=headers)).json()
            missing = await http.get("/api/incidents/INC404", headers=headers)
            stats = (await http.get("/api/sops/stats", headers=headers)).json()["items"]

        kpi = summary["kpi"]
        assert kpi["incidents_received"] == 5
        assert kpi["incidents_resolved"] == 1 and kpi["incidents_escalated"] == 4
        assert kpi["incidents_needs_human"] == 0 and kpi["currently_processing"] == 0
        assert kpi["resolution_rate_pct"] == 20.0 and kpi["avg_resolution_ms"] is not None
        assert sum(d["received"] for d in summary["trend_7day"]) == 5
        assert {r["reason"] for r in summary["escalation_reasons"]} <= {
            "no_sop",
            "low_confidence",
            "sop_execution",
        }
        assert sum(r["count"] for r in summary["escalation_reasons"]) == 4

        assert listing["total"] == 5 and "step_results" not in listing["items"][0]
        assert listing["items"][0]["incident_number"] == numbers[-1], "newest first"
        assert [r["incident_number"] for r in resolved["items"]] == [numbers[1]]
        assert search["total"] == 1
        assert len(page["items"]) == 2 and page["page"] == 2
        assert detail["outcome"] == "resolved" and detail["verified_by"] == "step-verify"
        assert len(detail["step_results"]) == detail["step_count"] > 0
        assert missing.status_code == 404
        mq = next(s for s in stats if s["sop_id"] == "SOP-MQ-001")
        assert mq["total_runs"] == 2 and mq["resolved_count"] == 1

    async def test_audit_api_returns_a_run_timeline_and_verifies_the_chain(self, backend):
        app, ctx = await build(backend)
        numbers = await run_incidents(ctx, ctx.tenant_id, [1])
        headers = await token_headers(ctx, *ALL_READ)
        async with client(app) as http:
            run_id = (await http.get(f"/api/incidents/{numbers[0]}", headers=headers)).json()[
                "run_id"
            ]
            timeline = (
                await http.get(f"/api/audit/events?run_id={run_id}", headers=headers)
            ).json()
            everything = (await http.get("/api/audit/events?limit=500", headers=headers)).json()
            verified = (await http.get("/api/audit/verify", headers=headers)).json()
        types = [e["event_type"] for e in timeline["items"]]
        assert types[0] == "incident.received" and types[-1] == "run.concluded"
        assert all(e["correlation_id"] == run_id for e in timeline["items"])
        assert "token.created" in [e["event_type"] for e in everything["items"]]
        assert verified["ok"] is True and verified["problems"] == []
        assert verified["events_checked"] == len(everything["items"])

    async def test_openapi_document_is_served_to_a_holder_of_system_read(self, backend):
        app, ctx = await build(backend)
        async with client(app) as http:
            anonymous = await http.get("/api/openapi.json")
            viewer = await http.get(
                "/api/openapi.json", headers=await token_headers(ctx, "incidents.read")
            )
            operator = await http.get(
                "/api/openapi.json", headers=await token_headers(ctx, "system.read")
            )
        assert [anonymous.status_code, viewer.status_code, operator.status_code] == [401, 403, 200]
        assert "/api/incidents" in operator.json()["paths"]


class TestEventStream:
    async def test_stream_replays_new_events_for_the_callers_tenant_only(
        self, backend, monkeypatch
    ):
        monkeypatch.setattr(app_module, "STREAM_POLL_SECONDS", 0.01)
        app, ctx = await build(backend)
        other = await backend.new_tenant()
        headers = await token_headers(ctx, "incidents.read")

        # The stream starts from "now": events are produced while it is open.
        calls = {"n": 0}

        async def disconnected(self) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                await run_incidents(ctx, ctx.tenant_id, [1])
                await run_incidents(ctx, other, [0])
            return calls["n"] > 3

        monkeypatch.setattr("starlette.requests.Request.is_disconnected", disconnected)
        async with client(app) as http:
            resp = await http.get("/api/stream", headers=headers)

        assert resp.headers["content-type"].startswith("text/event-stream")
        events = [
            json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")
        ]
        types = [e["type"] for e in events]
        assert types[0] == "heartbeat"
        assert "step_executing" in types and "step_done" in types and "incident_resolved" in types
        numbers = {e.get("incident_number") for e in events} - {None}
        assert numbers == {DEMO_INCIDENTS[1]["number"]}, "nothing from the other tenant"


class TestTenantIsolationOverHttp:
    """Exit criterion 2: every route, with tenant A's credentials, shows nothing of tenant B."""

    @pytest.fixture
    async def two(self, backend):
        app, ctx = await build(backend)
        b = await backend.new_tenant("b")
        a_numbers = await run_incidents(ctx, ctx.tenant_id, [1])
        b_numbers = await run_incidents(ctx, b, [0, 3])
        await store.set_role_mapping(ctx.db, b, "b-admins", "tenant_admin", ADMIN, grant=True)
        await store.create_api_token(ctx.db, b, "b-token", ["incidents.read"], 1, ADMIN)
        # An SOP version and a connection in each tenant. The run numbers index them, so
        # the walk below asks tenant A for tenant B's version by its id.
        self.versions = {}
        self.actions = {}
        for tenant_id, numbers, label in ((ctx.tenant_id, a_numbers, "a"), (b, b_numbers, "b")):
            for number in numbers:
                self.actions[number] = await seed_action(
                    ctx.db, tenant_id, number, f"{label.upper()}_ONLY_JOB"
                )
                await seed_model(ctx.db, tenant_id, number, f"{label}-only-region")
                self.versions[number] = await sop_store.create_draft(
                    ctx.db,
                    tenant_id,
                    ADMIN,
                    {"sop_id": f"SOP-{label}-{number}", "title": f"{label}-only-sop", "steps": []},
                )
        await connections.set_connection(
            ctx.db, b, ADMIN, "splunk", {"base_url": "https://b-splunk.example"}, {}
        )
        self.b_runner = await seed_runner(ctx.db, b, "b-only-runner")
        await seed_runner(ctx.db, ctx.tenant_id, "a-runner")
        return app, ctx, b, a_numbers, b_numbers

    async def test_every_read_route_returns_only_the_callers_tenant(self, two):
        app, ctx, b, a_numbers, b_numbers = two
        cookies, _ = await session(
            ctx, ["tenant_admin", "auditor", "operator"], tenant_id=ctx.tenant_id
        )
        markers = [
            *b_numbers,
            "b-admins",
            "b-token",
            "b-only-sop",
            "b-splunk.example",
            "B_ONLY_JOB",
            "b-only-region",
            "b-only-runner",
            str(b),
            DEMO_INCIDENTS[0]["short_description"],
        ]
        checked = 0
        async with client(app) as http:
            http.cookies.update(cookies)
            for route in app.routes:
                access = access_of(route)
                if "GET" not in route.methods or access.kind == AccessKind.PUBLIC:
                    continue
                if route.path in ("/api/stream", "/metrics", "/api/status", "/api/openapi.json"):
                    continue  # stream tested above; the others carry no tenant data
                for number in [*b_numbers, a_numbers[0]]:
                    path = route.path.replace("{incident_number}", number)
                    path = path.replace("{version_id}", str(self.versions[number]))
                    path = path.replace("{action_id}", str(self.actions[number]))
                    resp = await http.get(path, params={"limit": 500, "per_page": 100})
                    if "{" in route.path and number in b_numbers:
                        assert resp.status_code == 404, path
                    else:
                        assert resp.status_code == 200, path
                    for marker in markers:
                        assert marker not in resp.text, f"{path} leaked {marker}"
                    checked += 1
        assert checked >= 20

    async def test_tenant_b_sees_its_own_data_so_the_check_is_not_vacuous(self, two):
        app, ctx, b, _, b_numbers = two
        headers = await token_headers(ctx, "incidents.read", tenant_id=b)
        async with client(app) as http:
            listing = (await http.get("/api/incidents", headers=headers)).json()
        assert {r["incident_number"] for r in listing["items"]} == set(b_numbers)

    async def test_write_routes_act_on_the_callers_tenant_only(self, two):
        app, ctx, b, _, _ = two
        cookies, csrf = await session(ctx, ["tenant_admin"])
        h = {"X-CSRF-Token": csrf}
        b_token_id = (await store.list_api_tokens(ctx.db, b))[0]["id"]
        async with client(app) as http:
            http.cookies.update(cookies)
            revoke = await http.delete(f"/api/tokens/{b_token_id}", headers=h)
            remove = await http.delete(
                "/api/role-mappings",
                params={"group": "b-admins", "role": "tenant_admin"},
                headers=h,
            )
            runner = await http.delete(f"/api/runners/{self.b_runner}", headers=h)
        assert revoke.status_code == 404 and remove.status_code == 404
        assert runner.status_code == 404
        assert [r["revoked"] for r in await runner_store.list_runners(ctx.db, b)] == [False]
        assert (await store.list_api_tokens(ctx.db, b))[0]["revoked"] is False
        assert await store.list_role_mappings(ctx.db, b) == [
            {"group": "b-admins", "role": "tenant_admin"}
        ]

    async def test_credential_with_another_tenants_prefix_is_refused(self, two):
        app, ctx, b, _, _ = two
        headers = await token_headers(ctx, "incidents.read")
        token = headers["Authorization"].split()[1]
        _, tenant_hex, secret = token.split("_", 2)
        swapped = f"l1t_{b.hex}_{secret}"
        cookies, _ = await session(ctx, ["viewer"])
        cookie = cookies["l1_session"]
        async with client(app) as http:
            by_token = await http.get(
                "/api/incidents", headers={"Authorization": f"Bearer {swapped}"}
            )
            http.cookies.set("l1_session", f"{b.hex}.{cookie.split('.')[1]}")
            by_cookie = await http.get("/api/incidents")
        assert by_token.status_code == 401 and by_cookie.status_code == 401

    async def test_webhook_incident_lands_in_the_deployment_tenant(self, two):
        from tests.api.support import PAYLOAD, signed_headers

        app, ctx, b, _, _ = two
        body = json.dumps(PAYLOAD).encode()
        async with client(app) as http:
            await http.post("/webhook/incident", content=body, headers=signed_headers(body))
            mine = await http.get(
                f"/api/incidents/{PAYLOAD['number']}",
                headers=await token_headers(ctx, "incidents.read"),
            )
            theirs = await http.get(
                f"/api/incidents/{PAYLOAD['number']}",
                headers=await token_headers(ctx, "incidents.read", tenant_id=b),
            )
        await ctx.runs.work_once([ctx.tenant_id])
        assert theirs.status_code == 404
        assert mine.status_code == 404, "not concluded yet, so no record"
        async with client(app) as http:
            done = await http.get(
                f"/api/incidents/{PAYLOAD['number']}",
                headers=await token_headers(ctx, "incidents.read"),
            )
        assert done.status_code == 200
