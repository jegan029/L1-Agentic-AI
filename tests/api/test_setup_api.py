"""Set-up over HTTP: the onboarding checklist, the connector list, and the ITSM connection."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest

from src.l1_agent.actions import store as action_store
from src.l1_agent.audit.log import Actor
from src.l1_agent.auth import store as auth_store
from src.l1_agent.config.settings import (
    AgentSettings,
    ConfigError,
    LLMSettings,
    RunnerSettings,
    Settings,
)
from src.l1_agent.connectors import hub as connections
from src.l1_agent.connectors.catalog import default_registry
from src.l1_agent.connectors.itsm.handle import ItsmHandle, ItsmNotConnected
from src.l1_agent.connectors.sdk.manifest import Kind, RiskTier
from src.l1_agent.connectors.sdk.secrets import SecretResolver
from src.l1_agent.main import L1AgentService
from src.l1_agent.runner import keys
from src.l1_agent.runner import store as runner_store
from tests.api.support import PAYLOAD, build, client, session, signed_headers
from tests.e2e.fake_servicenow import FakeServiceNow

ADMIN = Actor("user", "https://idp.test|admin-1")


def steps_of(report: dict) -> dict:
    return {s["id"]: s for s in report["steps"]}


class TestOnboarding:
    async def test_a_new_tenant_has_everything_still_to_do_and_says_how(self, backend):
        app, ctx = await build(backend)
        cookies, _ = await session(ctx, ["operator"])
        async with client(app) as http:
            http.cookies.update(cookies)
            report = (await http.get("/api/onboarding")).json()
        steps = steps_of(report)
        assert report["complete"] is False and report["runner_mode"] == "embedded"
        assert "runner" not in steps, "no runner step when calls are made in the worker"
        for step_id in ("sign_in", "itsm", "sop", "policy", "first_run"):
            assert steps[step_id]["done"] is False and steps[step_id]["how"], step_id
        assert steps["knowledge"]["required"] is False
        assert steps["policy"]["detail"] == "shadow (the default; never set)"
        assert report["total"] == sum(1 for s in report["steps"] if s["required"])

    async def test_a_step_is_done_when_the_thing_exists_and_undone_when_it_is_removed(
        self, backend
    ):
        app, ctx = await build(backend)
        tenant = ctx.tenant_id
        cookies, _ = await session(ctx, ["tenant_admin"])
        async with client(app) as http:
            http.cookies.update(cookies)
            await action_store.set_policy(ctx.db, tenant, ADMIN, autonomy="shadow")
            await auth_store.set_role_mapping(ctx.db, tenant, "ops", "operator", ADMIN, grant=True)
            steps = steps_of((await http.get("/api/onboarding")).json())
            assert steps["policy"]["done"] is True and steps["policy"]["detail"] == "shadow"
            # Mapped groups are not enough without an identity provider to sign in through.
            assert steps["sign_in"]["done"] is False

            await connections.set_connection(
                ctx.db, tenant, ADMIN, "git_knowledge", {"url": "https://git.example/sops.git"}, {}
            )
            assert steps_of((await http.get("/api/onboarding")).json())["knowledge"]["done"] is True
            await connections.remove_connection(ctx.db, tenant, ADMIN, "git_knowledge")
            assert (
                steps_of((await http.get("/api/onboarding")).json())["knowledge"]["done"] is False
            )

    async def test_it_needs_the_permission_to_read_the_system(self, backend):
        app, ctx = await build(backend)
        for roles, expected in ((["viewer"], 403), (["sop_author"], 403), (["operator"], 200)):
            cookies, _ = await session(ctx, roles)
            async with client(app) as http:
                http.cookies.update(cookies)
                assert (await http.get("/api/onboarding")).status_code == expected, roles
                assert (await http.get("/api/runners")).status_code == expected, roles

    async def test_with_a_runner_the_step_follows_whether_one_is_connected(self, backend):
        from tests.runner_support import make_remote

        remote = await make_remote(backend)
        try:
            cookies, _ = await session(remote.ctx, ["tenant_admin"])
            async with client(remote.app) as http:
                http.cookies.update(cookies)
                before = steps_of((await http.get("/api/onboarding")).json())["runner"]
                assert before["done"] is False and before["detail"] == "1 enrolled, 0 connected"
                await remote.make_runner().poll_once(wait_seconds=0)
                after = steps_of((await http.get("/api/onboarding")).json())["runner"]
                assert after["done"] is True and after["detail"] == "1 enrolled, 1 connected"
                await runner_store.revoke(remote.db, remote.tenant, ADMIN, remote.runner_id)
                gone = steps_of((await http.get("/api/onboarding")).json())["runner"]
                assert gone["done"] is False and gone["detail"] == "0 enrolled, 0 connected"
        finally:
            await remote.stop()


class TestConnectorList:
    async def test_it_shows_stored_settings_and_references_and_where_calls_are_made(self, backend):
        app, ctx = await build(backend)
        await connections.set_connection(
            ctx.db,
            ctx.tenant_id,
            ADMIN,
            "splunk",
            {"base_url": "https://splunk.example", "search_timeout_seconds": 60},
            {"token": "vault:splunk/token"},
        )
        ctx.hub.forget(ctx.tenant_id)
        cookies, _ = await session(ctx, ["operator"])
        async with client(app) as http:
            http.cookies.update(cookies)
            items = {c["id"]: c for c in (await http.get("/api/connectors")).json()["items"]}
        splunk = items["splunk"]
        assert splunk["configured"] and splunk["origin"] == "connection"
        assert splunk["settings"] == {
            "base_url": "https://splunk.example",
            "search_timeout_seconds": 60,
        }
        assert splunk["credentials"] == {"token": "vault:splunk/token"}
        assert splunk["config"]["properties"]["base_url"] == {"type": "string"}, "the schema"
        assert splunk["runs_on"] == "control_plane", "no runner in this deployment"
        assert items["dynatrace"]["configured"] is False and items["dynatrace"]["settings"] == {}

    def test_preview_connectors_are_read_only(self):
        """Decision D7: the mainframe and browser connectors ship as preview, T0 only."""
        registry = default_registry()
        preview = {m.id: m for m in registry.manifests() if m.preview}
        assert set(preview) == {"browser", "mainframe"}
        for manifest in preview.values():
            assert manifest.capabilities, manifest.id
            for capability in manifest.capabilities:
                assert capability.kind is Kind.READ and capability.tier is RiskTier.T0, (
                    capability.id
                )
        writes = [c for c in registry.capability_ids() if registry.capability(c).kind is Kind.WRITE]
        assert all(registry.owner(c) not in preview for c in writes)


@pytest.fixture
async def snow(tmp_path):
    fake = await FakeServiceNow(tmp_path).start()
    yield fake
    await fake.close()


def demo_settings() -> Settings:
    settings = Settings(agent=AgentSettings(demo_mode=True), llm=LLMSettings(enabled=False))
    return replace(settings, servicenow=replace(settings.servicenow, verify_ssl=False))


class TestTheItsmConnection:
    def handle(self, backend, tenant: uuid.UUID, fallback=None) -> ItsmHandle:
        return ItsmHandle(
            backend.db,
            tenant,
            demo_settings(),
            SecretResolver.from_settings(demo_settings().secrets),
            lambda: fallback,
            refresh_seconds=0,
        )

    def connection(self, snow, tmp_path, **changes) -> tuple:
        (tmp_path / "pw").write_text("pw", encoding="utf-8")
        config = {
            "base_url": snow.url,
            "auth": "basic",
            "username": "agent",
            "assignment_group": "L1-Batch-Support",
        }
        config.update(changes)
        return config, {"password": "file:" + (tmp_path / "pw").as_posix()}

    async def test_with_no_connection_every_call_says_so(self, backend):
        tenant = await backend.new_tenant()
        handle = self.handle(backend, tenant)
        assert await handle.current() is None
        assert await handle.reason() == "No ServiceNow connection is configured"
        with pytest.raises(ItsmNotConnected):
            await handle.add_work_note("x", "note")
        checks = await handle.test()
        assert [c.ok for c in checks] == [False]

    async def test_an_entered_connection_is_used_without_a_restart_and_dropped_when_removed(
        self, backend, snow, tmp_path
    ):
        tenant = await backend.new_tenant()
        handle = self.handle(backend, tenant)
        config, refs = self.connection(snow, tmp_path)
        await connections.set_connection(backend.db, tenant, ADMIN, "servicenow", config, refs)
        try:
            assert await handle.reason() == ""
            assert [c.ok for c in await handle.test()] == [True, True]
            snow.add_incident({"sys_id": "c" * 32, "number": "INC1", "state": "1"})
            await handle.add_work_note("c" * 32, "written through the handle")
            assert snow.work_notes["c" * 32] == ["written through the handle"]
            assert snow.refused == 0, "the password was read from its reference"

            await connections.remove_connection(backend.db, tenant, ADMIN, "servicenow")
            assert await handle.current() is None
        finally:
            await handle.close()

    async def test_a_stored_connection_wins_and_a_broken_one_does_not_fall_back(
        self, backend, snow, tmp_path
    ):
        tenant = await backend.new_tenant()
        from_environment = object()
        handle = self.handle(backend, tenant, fallback=from_environment)
        assert await handle.current() is from_environment

        config, refs = self.connection(snow, tmp_path, assignment_group="")
        await connections.set_connection(backend.db, tenant, ADMIN, "servicenow", config, refs)
        assert await handle.current() is None, "not the environment's: that would be a surprise"
        assert "SERVICENOW_ASSIGNMENT_GROUP" in await handle.reason()

        config, refs = self.connection(snow, tmp_path)
        await connections.set_connection(
            backend.db, tenant, ADMIN, "servicenow", config, refs, enabled=False
        )
        assert await handle.current() is from_environment, "switched off: as if not stored"

    async def test_a_wrong_password_fails_the_test_and_names_the_step(
        self, backend, snow, tmp_path
    ):
        tenant = await backend.new_tenant()
        handle = self.handle(backend, tenant)
        config, refs = self.connection(snow, tmp_path)
        (tmp_path / "pw").write_text("wrong", encoding="utf-8")
        await connections.set_connection(backend.db, tenant, ADMIN, "servicenow", config, refs)
        try:
            checks = await handle.test()
            assert [c.ok for c in checks] == [False, False]
            assert "401" in checks[0].detail and "wrong" not in json.dumps(
                [c.detail for c in checks]
            )
        finally:
            await handle.close()

    async def test_the_webhook_takes_nothing_until_there_is_somewhere_to_write_back(
        self, backend, snow, tmp_path
    ):
        app, ctx = await build(backend)
        ctx.itsm = self.handle(backend, ctx.tenant_id)
        body = json.dumps(PAYLOAD).encode()
        async with client(app) as http:
            early = await http.post("/webhook/incident", content=body, headers=signed_headers(body))
            assert early.status_code == 503
            config, refs = self.connection(snow, tmp_path)
            await connections.set_connection(
                backend.db, ctx.tenant_id, ADMIN, "servicenow", config, refs
            )
            body = json.dumps({**PAYLOAD, "number": "INC0000002", "sys_id": "abc124"}).encode()
            later = await http.post("/webhook/incident", content=body, headers=signed_headers(body))
            assert later.status_code == 200 and later.json()["status"] == "accepted"
        await ctx.itsm.close()


class TestServiceAssembly:
    def remote_settings(self, tmp_path, **runner) -> Settings:
        url = f"sqlite+aiosqlite:///{(tmp_path / 'svc.db').as_posix()}"
        settings = Settings(
            agent=AgentSettings(demo_mode=True, demo_seed=False), llm=LLMSettings(enabled=False)
        )
        return replace(
            settings,
            database=replace(settings.database, url=url, tenant_slug="demo"),
            runner=RunnerSettings(mode="remote", **runner),
        )

    async def test_remote_mode_needs_the_key_calls_are_signed_with(self, tmp_path):
        service = L1AgentService(self.remote_settings(tmp_path))
        try:
            with pytest.raises(ConfigError, match="RUNNER_DISPATCH_KEY_REF"):
                await service.setup()
        finally:
            await service.close()

    async def test_remote_mode_publishes_only_public_keys_and_holds_no_mock_itself(self, tmp_path):
        dispatch_pem, dispatch_public = keys.generate()
        grant_pem, grant_public = keys.generate()
        service = L1AgentService(
            self.remote_settings(tmp_path, dispatch_key=dispatch_pem, grant_key=grant_pem)
        )
        try:
            ctx = await service.setup()
            assert ctx.runner_keys == {"dispatch": dispatch_public, "grant": grant_public}
            assert ctx.hub.remote is True
            # With a runner, even demo mode leaves the mocks to the runner.
            listed = {c["id"]: c for c in await ctx.hub.describe(ctx.tenant_id)}
            assert (
                listed["splunk"]["configured"] is False and listed["splunk"]["runs_on"] == "runner"
            )
            assert listed["servicenow"]["runs_on"] == "control_plane"
        finally:
            await service.close()

    async def test_without_a_grant_key_the_service_starts_and_shows_none(self, tmp_path):
        service = L1AgentService(self.remote_settings(tmp_path, dispatch_key=keys.generate()[0]))
        try:
            ctx = await service.setup()
            assert ctx.runner_keys["grant"] == "" and ctx.runner_keys["dispatch"]
        finally:
            await service.close()

    async def test_an_unseeded_demo_tenant_starts_empty(self, tmp_path):
        from src.l1_agent.sop import store as sop_store

        settings = self.remote_settings(tmp_path)
        service = L1AgentService(replace(settings, runner=RunnerSettings()))
        try:
            ctx = await service.setup()
            assert await sop_store.list_versions(ctx.db, ctx.tenant_id) == []
            assert (await action_store.get_policy(ctx.db, ctx.tenant_id)).updated_at is None
        finally:
            await service.close()

    def test_a_listener_certificate_needs_its_key(self):
        from src.l1_agent.api.security import SecurityConfigError, validate_security_config

        base = Settings(agent=AgentSettings(demo_mode=True), llm=LLMSettings(enabled=False))
        for cert, key in (("/tls/tls.crt", ""), ("", "/tls/tls.key")):
            broken = replace(base, agent=replace(base.agent, tls_cert_file=cert, tls_key_file=key))
            with pytest.raises(SecurityConfigError, match="set together"):
                validate_security_config(broken)
        both = replace(base, agent=replace(base.agent, tls_cert_file="/c", tls_key_file="/k"))
        validate_security_config(both)

    def test_public_keys_can_be_given_as_files(self, tmp_path, monkeypatch):
        public = keys.generate()[1]
        path = tmp_path / "dispatch.key.pub"
        path.write_text(public + "\n", encoding="ascii")
        monkeypatch.setenv("RUNNER_DISPATCH_PUBLIC_KEY_FILE", str(path))
        assert Settings.from_env().runner.dispatch_public_key == public
        monkeypatch.setenv("RUNNER_DISPATCH_PUBLIC_KEY_FILE", str(tmp_path / "missing"))
        with pytest.raises(ConfigError, match="RUNNER_DISPATCH_PUBLIC_KEY_FILE"):
            Settings.from_env()
