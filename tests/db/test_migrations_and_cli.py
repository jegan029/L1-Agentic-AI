"""Migrations match the schema in code, and the command-line tool does what it prints."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from src.l1_agent import cli
from src.l1_agent.audit.log import Actor, append, verify
from src.l1_agent.auth import store
from src.l1_agent.config.settings import (
    AgentSettings,
    AuthSettings,
    ConfigError,
    DatabaseSettings,
    LLMSettings,
    Settings,
)
from src.l1_agent.db.migrate import alembic_config, upgrade
from src.l1_agent.db.models import metadata, runs
from src.l1_agent.demo import DEMO_INCIDENTS
from src.l1_agent.main import L1AgentService
from src.l1_agent.models.incident import Incident
from src.l1_agent.service.runs import record_by_number

KEY = "k" * 40


class TestMigrations:
    async def test_migrated_schema_has_no_drift_from_the_models(self, pg_urls):
        engine = create_async_engine(pg_urls.owner)
        try:
            async with engine.connect() as conn:
                diff = await conn.run_sync(
                    lambda sync: compare_metadata(
                        MigrationContext.configure(sync, opts={"compare_type": True}), metadata
                    )
                )
        finally:
            await engine.dispose()
        assert diff == [], "models changed without a migration"

    async def test_running_migrations_again_changes_nothing(self, pg_urls):
        await asyncio.to_thread(upgrade, pg_urls.owner)

    def test_bad_arguments_are_refused(self):
        with pytest.raises(ConfigError, match="postgresql"):
            alembic_config("sqlite+aiosqlite:///x.db")
        for role in ("l1_app; DROP ROLE postgres", "L1App", "", "a" * 64):
            with pytest.raises(ConfigError, match="role name"):
                alembic_config("postgresql+asyncpg://u:p@h/d", role)

    def test_migrations_are_forward_only(self):
        import importlib.util
        from pathlib import Path

        import src.l1_agent.db.migrate as migrate

        for path in sorted(
            (Path(migrate.__file__).parent / "migrations" / "versions").glob("*.py")
        ):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with pytest.raises(RuntimeError, match="forward only"):
                module.downgrade()


def settings_for(pg_urls, slug: str, **auth) -> Settings:
    return Settings(
        agent=AgentSettings(demo_mode=False, webhook_secret="s" * 40),
        llm=LLMSettings(enabled=False),
        database=DatabaseSettings(url=pg_urls.app, migration_url=pg_urls.owner, tenant_slug=slug),
        auth=AuthSettings(**auth),
    )


async def run_cli(settings: Settings, *argv: str) -> int:
    return await cli._run(cli._parser().parse_args(list(argv)), settings)


class TestCommandLine:
    @pytest.fixture
    async def env(self, pg, pg_urls):
        slug = f"cli-{uuid.uuid4().hex[:8]}"
        tenant_id = await pg._owner.ensure_tenant(slug)
        return pg, settings_for(pg_urls, slug, audit_checkpoint_key=KEY), tenant_id

    async def test_tenant_create_uses_the_migration_role_and_is_audited(self, pg, pg_urls, capsys):
        slug = f"new-{uuid.uuid4().hex[:8]}"
        settings = settings_for(pg_urls, slug)
        assert await run_cli(settings, "tenant", "create", "--slug", slug, "--name", "New") == 0
        tenant_id = await pg.db.tenant_id_for(slug)
        assert str(tenant_id) in capsys.readouterr().out
        result = await verify(pg.db, tenant_id)
        assert result.ok and result.events_checked == 1

        no_owner = replace(settings, database=replace(settings.database, migration_url=""))
        with pytest.raises(ConfigError, match="DATABASE_MIGRATION_URL"):
            await run_cli(no_owner, "tenant", "create", "--slug", "x")

    async def test_unknown_tenant_is_an_error(self, pg, pg_urls):
        with pytest.raises(ConfigError, match="does not exist"):
            await run_cli(settings_for(pg_urls, "no-such-tenant"), "roles", "list")

    async def test_roles_and_tokens(self, env, capsys):
        pg, settings, tenant_id = env
        assert (
            await run_cli(settings, "roles", "grant", "--group", "ops", "--role", "operator") == 0
        )
        assert await run_cli(settings, "roles", "list") == 0
        assert "ops\toperator" in capsys.readouterr().out
        with pytest.raises(ValueError, match="Unknown role"):
            await run_cli(settings, "roles", "grant", "--group", "ops", "--role", "root")

        code = await run_cli(
            settings,
            "token",
            "create",
            "--name",
            "ci",
            "--permission",
            "incidents.read",
            "--days",
            "7",
        )
        out = capsys.readouterr().out
        token = next(line.split()[1] for line in out.splitlines() if line.startswith("token "))
        token_id = next(line.split()[1] for line in out.splitlines() if line.startswith("id "))
        principal = await store.load_api_token(pg.db, token)
        assert code == 0 and principal.tenant_id == tenant_id and principal.kind == "token"

        assert await run_cli(settings, "token", "revoke", "--id", token_id) == 0
        assert await store.load_api_token(pg.db, token) is None
        assert await run_cli(settings, "token", "revoke", "--id", token_id) == 1

    async def test_audit_verify_exit_status_follows_the_chain(self, env, tmp_path, capsys):
        pg, settings, tenant_id = env
        for i in range(4):
            async with pg.db.tenant(tenant_id) as conn:
                await append(conn, tenant_id, Actor.system("t"), "test.event", {"n": i})

        assert await run_cli(settings, "audit", "verify") == 0
        assert await run_cli(settings, "audit", "checkpoint") == 0
        lines = capsys.readouterr().out.splitlines()
        saved = tmp_path / "checkpoints.jsonl"
        saved.write_text(
            next(line for line in lines if line.startswith("{")) + "\n", encoding="utf-8"
        )
        assert json.loads(saved.read_text())["seq"] == 4

        # Someone with database ownership removes the newest events.
        await pg.tamper("DELETE FROM audit_events WHERE tenant_id = $1 AND seq > 2", tenant_id)
        assert await run_cli(settings, "audit", "verify") == 0, "a cut-off chain alone looks intact"
        assert await run_cli(settings, "audit", "verify", "--checkpoints", str(saved)) == 2
        assert "PROBLEM" in capsys.readouterr().out

        await pg.tamper("DELETE FROM audit_events WHERE tenant_id = $1 AND seq = 1", tenant_id)
        assert await run_cli(settings, "audit", "verify") == 2

    async def test_checkpoint_needs_a_key(self, env):
        _, settings, _ = env
        with pytest.raises(ConfigError, match="AUDIT_CHECKPOINT_KEY"):
            await run_cli(replace(settings, auth=AuthSettings()), "audit", "checkpoint")


class FakePollingServiceNow:
    """Returns the same new incidents on every poll, as a real instance does until they move."""

    def __init__(self, service: L1AgentService, polls: int) -> None:
        self._service = service
        self._left = polls
        self.notes: list = []

    async def get_new_incidents(self, **_) -> list:
        self._left -= 1
        if self._left <= 0:
            self._service._stop.set()
        return [
            Incident.from_servicenow(DEMO_INCIDENTS[1]),
            Incident.from_servicenow(DEMO_INCIDENTS[0]),
        ]

    async def add_work_note(self, sys_id: str, note: str) -> dict:
        self.notes.append(note)
        return {}

    async def update_incident(self, sys_id: str, fields: dict) -> dict:
        return {}

    async def resolve_incident(self, sys_id: str, close_notes: str) -> dict:
        return {}

    async def get_sops(self, **_) -> list:
        return []

    async def close(self) -> None:
        pass


class TestServiceLoops:
    async def test_poller_queues_each_incident_once_and_workers_finish_them(self, tmp_path):
        url = f"sqlite+aiosqlite:///{(tmp_path / 'svc.db').as_posix()}"
        settings = Settings(
            agent=AgentSettings(demo_mode=True, max_concurrent_incidents=2),
            llm=LLMSettings(enabled=False),
            database=DatabaseSettings(url=url, tenant_slug="demo"),
        )
        settings = replace(
            settings,
            servicenow=replace(settings.servicenow, poll_interval_seconds=0),
            worker=replace(settings.worker, poll_interval_seconds=0.01),
        )
        service = L1AgentService(settings)
        service._snow_client = FakePollingServiceNow(service, polls=4)
        try:
            ctx = await service.setup()
            # setup built the processor with the stand-in; point it at the polling fake
            ctx.runs._processor._snow = service._snow_client
            await service._poll_loop(ctx)  # returns when the fake has been polled four times

            service._stop.clear()
            workers = service.start_workers(ctx)
            for _ in range(200):
                done = await record_by_number(ctx.db, ctx.tenant_id, DEMO_INCIDENTS[0]["number"])
                if done and await record_by_number(
                    ctx.db, ctx.tenant_id, DEMO_INCIDENTS[1]["number"]
                ):
                    break
                await asyncio.sleep(0.02)
            service._stop.set()
            await asyncio.gather(*workers)

            async with ctx.db.tenant(ctx.tenant_id) as conn:
                outcomes = sorted(r.outcome for r in await conn.execute(sa.select(runs.c.outcome)))
            assert outcomes == ["escalated", "resolved"], "four polls, two incidents, two runs"
            assert sum("Incident received" in n for n in service._snow_client.notes) == 2
        finally:
            await service.close()

    async def test_service_refuses_a_postgres_tenant_that_does_not_exist(self, pg_urls):
        service = L1AgentService(
            replace(
                settings_for(pg_urls, "never-created"),
                agent=AgentSettings(demo_mode=True, webhook_secret="s" * 40),
            )
        )
        try:
            with pytest.raises(ConfigError, match="does not exist"):
                await service.setup()
        finally:
            await service.close()
