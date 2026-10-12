"""The model policy over HTTP: who may see it and who may change it (ADR 0009)."""

from __future__ import annotations

import json

from src.l1_agent.ai.providers import DeterministicProvider, OpenAICompatibleProvider
from src.l1_agent.config.settings import LLMSettings
from tests.api.support import audit_types, build, client, seed_model, session, token_headers
from tests.api.test_api_data import run_incidents

RULES = {"providers": ["deterministic"], "tasks": ["rank", "interpret"], "regions": ["local"]}


async def model_events(ctx) -> list:
    return [t for t in await audit_types(ctx) if t.startswith("model.")]


async def signed_in(ctx, *roles: str) -> dict:
    cookies, csrf = await session(ctx, list(roles))
    return {"cookies": cookies, "headers": {"X-CSRF-Token": csrf}}


class TestReading:
    async def test_a_new_tenant_uses_no_model_and_the_page_says_why(self, backend):
        app, ctx = await build(backend, provider=DeterministicProvider())
        user = await signed_in(ctx, "operator")
        async with client(app) as http:
            shown = (await http.get("/api/model", cookies=user["cookies"])).json()
        assert shown["enabled"] is False and shown["rules"] == {}
        assert shown["provider"]["provider"] == "deterministic"
        assert set(shown["refused"].values()) == {"model_off"}
        assert shown["prompts"] == {
            "rank": "rank/2",
            "extract": "extract/1",
            "interpret": "interpret/1",
            "draft": "draft/1",
        }
        assert shown["usage_today"]["calls"] == 0

    async def test_a_deployment_with_no_provider_says_so(self, backend):
        app, ctx = await build(backend)
        user = await signed_in(ctx, "operator")
        async with client(app) as http:
            shown = (await http.get("/api/model", cookies=user["cookies"])).json()
        assert shown["provider"] == {"configured": False} and shown["refused"] is None

    async def test_the_key_is_never_shown(self, backend):
        provider = OpenAICompatibleProvider(
            LLMSettings(
                endpoint="https://m.example/v1", api_key="sk-the-real-key-value", region="eu"
            )
        )
        app, ctx = await build(backend, provider=provider)
        user = await signed_in(ctx, "tenant_admin")
        async with client(app) as http:
            text = (await http.get("/api/model", cookies=user["cookies"])).text
        await provider.close()
        assert "sk-the-real-key-value" not in text and "m.example" not in text
        assert json.loads(text)["provider"]["region"] == "eu"


class TestChanging:
    async def test_the_tenant_admin_switches_the_model_on_and_it_is_audited(self, backend):
        app, ctx = await build(backend, provider=DeterministicProvider())
        admin = await signed_in(ctx, "tenant_admin")
        async with client(app) as http:
            resp = await http.put("/api/model", json={"enabled": True, "rules": RULES}, **admin)
        assert resp.status_code == 200, resp.text
        shown = resp.json()
        assert shown["enabled"] is True and shown["rules"] == RULES
        assert shown["refused"] == {
            "rank": None,
            "extract": "task_not_enabled",
            "interpret": None,
            "draft": "task_not_enabled",
        }
        assert await model_events(ctx) == ["model.policy_changed"]

    async def test_a_region_the_provider_is_not_in_shows_as_refused(self, backend):
        app, ctx = await build(backend, provider=DeterministicProvider())
        admin = await signed_in(ctx, "tenant_admin")
        rules = {**RULES, "regions": ["eu"]}
        async with client(app) as http:
            shown = (
                await http.put("/api/model", json={"enabled": True, "rules": rules}, **admin)
            ).json()
        assert shown["refused"]["rank"] == "region_not_allowed"

    async def test_an_invalid_policy_is_refused_with_every_problem(self, backend):
        app, ctx = await build(backend, provider=DeterministicProvider())
        admin = await signed_in(ctx, "tenant_admin")
        bad = {"providers": ["bedrock"], "tasks": ["decide"], "budgets": {"daily_tokens": -1}}
        async with client(app) as http:
            resp = await http.put("/api/model", json={"enabled": True, "rules": bad}, **admin)
            after = (await http.get("/api/model", cookies=admin["cookies"])).json()
        assert resp.status_code == 400 and len(resp.json()["problems"]) == 3
        assert after["enabled"] is False and await model_events(ctx) == []

    async def test_only_the_tenant_admin_may_change_it(self, backend):
        app, ctx = await build(backend, provider=DeterministicProvider())
        body = {"enabled": True, "rules": RULES}
        async with client(app) as http:
            for roles in (["operator"], ["approver"], ["auditor"], ["sop_author"], ["viewer"]):
                user = await signed_in(ctx, *roles)
                resp = await http.put("/api/model", json=body, **user)
                assert resp.status_code == 403, roles
            token = await token_headers(ctx, "system.read")
            assert (await http.put("/api/model", json=body, headers=token)).status_code == 403
            assert (await http.put("/api/model", json=body)).status_code == 401
        assert await model_events(ctx) == []

    async def test_a_change_without_the_csrf_header_is_refused(self, backend):
        app, ctx = await build(backend, provider=DeterministicProvider())
        admin = await signed_in(ctx, "tenant_admin")
        async with client(app) as http:
            resp = await http.put(
                "/api/model", json={"enabled": True, "rules": RULES}, cookies=admin["cookies"]
            )
        assert resp.status_code == 403


class TestTheRecordOfCalls:
    async def test_the_auditor_sees_digests_and_counts(self, backend):
        app, ctx = await build(backend)
        (number,) = await run_incidents(ctx, ctx.tenant_id, [1])
        await seed_model(ctx.db, ctx.tenant_id, number, "eu")
        auditor = await signed_in(ctx, "auditor")
        operator = await signed_in(ctx, "operator")
        async with client(app) as http:
            items = (await http.get("/api/model/calls", cookies=auditor["cookies"])).json()["items"]
            usage = (await http.get("/api/model", cookies=operator["cookies"])).json()[
                "usage_today"
            ]
            denied = await http.get("/api/model/calls", cookies=operator["cookies"])
            also_denied = await http.get("/api/model", cookies=auditor["cookies"])
        (call,) = items
        assert call["subject"] == number and call["task"] == "rank" and call["outcome"] == "ok"
        assert call["input_sha256"] == "1" * 64 and call["output_sha256"] == "2" * 64
        assert (call["input_tokens"], call["output_tokens"]) == (10, 5)
        assert usage == {**usage, "calls": 1, "tokens": 15, "by_outcome": {"ok": 1}}
        assert denied.status_code == 403, "the record of calls is the auditor's"
        assert also_denied.status_code == 403, "the policy is not"
