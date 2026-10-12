"""The command line an installer and an operator use: tenants, runners, onboarding, export."""

from __future__ import annotations

import uuid

import pytest

from src.l1_agent import cli
from src.l1_agent.audit.log import events_for, verify
from src.l1_agent.config.settings import ConfigError
from src.l1_agent.runner import keys, protocol
from src.l1_agent.runner import store as runner_store
from tests.db.test_migrations_and_cli import run_cli, settings_for


@pytest.fixture
async def env(pg, pg_urls):
    slug = f"p6-{uuid.uuid4().hex[:8]}"
    tenant_id = await pg._owner.ensure_tenant(slug)
    return pg, settings_for(pg_urls, slug), tenant_id


class TestTenantCreate:
    async def test_running_it_again_changes_nothing_and_writes_no_second_event(
        self, pg, pg_urls, capsys
    ):
        slug = f"again-{uuid.uuid4().hex[:8]}"
        settings = settings_for(pg_urls, slug)
        for _ in range(3):
            assert await run_cli(settings, "tenant", "create", "--slug", slug) == 0
        out = capsys.readouterr().out.splitlines()
        assert "(already exists)" not in out[0] and out[1].endswith("(already exists)")
        tenant_id = await pg.db.tenant_id_for(slug)
        types = [e["event_type"] for e in await events_for(pg.db, tenant_id)]
        assert types == ["tenant.created"]


class TestRunnerCommands:
    async def test_enrol_list_and_revoke(self, env, capsys):
        pg, settings, tenant_id = env
        public = keys.generate()[1]
        assert (
            await run_cli(settings, "runner", "enrol", "--name", "r1", "--public-key", public) == 0
        )
        out = capsys.readouterr().out
        runner_id = uuid.UUID(out.split("runner ")[1].split()[0])
        assert f"identity {protocol.identity(tenant_id, runner_id)}" in out

        assert await run_cli(settings, "runner", "list") == 0
        listed = capsys.readouterr().out
        assert "r1\tidle" in listed and str(runner_id) in listed

        assert await run_cli(settings, "runner", "revoke", "--id", str(runner_id)) == 0
        assert await run_cli(settings, "runner", "revoke", "--id", str(runner_id)) == 1
        assert await run_cli(settings, "runner", "list") == 0
        assert "r1\trevoked" in capsys.readouterr().out
        types = [e["event_type"] for e in await events_for(pg.db, tenant_id)]
        assert types == ["runner.enrolled", "runner.revoked"]
        assert (await verify(pg.db, tenant_id)).ok

    async def test_if_absent_is_for_an_installer_that_runs_more_than_once(
        self, env, tmp_path, capsys
    ):
        pg, settings, tenant_id = env
        public = keys.generate()[1]
        key_file = tmp_path / "runner.key.pub"
        key_file.write_text(public + "\n", encoding="ascii")
        enrol = ("runner", "enrol", "--name", "runner-1", "--public-key-file", str(key_file))
        for _ in range(3):
            assert await run_cli(settings, *enrol, "--if-absent") == 0
        assert len(await runner_store.list_runners(pg.db, tenant_id)) == 1
        assert [e["event_type"] for e in await events_for(pg.db, tenant_id)] == ["runner.enrolled"]

        # The same name with another key is not quietly accepted, with or without the flag.
        other = keys.generate()[1]
        with pytest.raises(ConfigError, match="different key"):
            await run_cli(
                settings,
                "runner",
                "enrol",
                "--name",
                "runner-1",
                "--public-key",
                other,
                "--if-absent",
            )
        with pytest.raises(ConfigError, match="already enrolled"):
            await run_cli(settings, *enrol)
        with pytest.raises(ConfigError, match="public key"):
            await run_cli(
                settings, "runner", "enrol", "--name", "x", "--public-key", "nope", "--if-absent"
            )

    def test_keygen_writes_both_halves_and_never_overwrites(self, tmp_path, capsys):
        target = tmp_path / "runner.key"
        assert cli.main(["runner", "keygen", "--out", str(target)]) == 0
        public = (tmp_path / "runner.key.pub").read_text(encoding="ascii").strip()
        assert keys.public_text(keys.public_of(keys.private_seed(target.read_text()))) == public
        printed = capsys.readouterr().out
        assert public in printed and "PRIVATE KEY" not in printed
        before = target.read_text()
        assert cli.main(["runner", "keygen", "--out", str(target)]) == 1
        assert target.read_text() == before


class TestExportCommand:
    async def test_create_then_verify(self, env, tmp_path, capsys):
        pg, settings, tenant_id = env
        public = keys.generate()[1]
        await run_cli(settings, "runner", "enrol", "--name", "r1", "--public-key", public)
        out = tmp_path / "export"
        assert await run_cli(settings, "export", "create", "--out", str(out)) == 0
        printed = capsys.readouterr().out
        assert "audit_events.jsonl" in printed and f"written to {out}" in printed
        assert cli.main(["export", "verify", "--dir", str(out)]) == 0
        with pytest.raises(ConfigError, match="not empty"):
            await run_cli(settings, "export", "create", "--out", str(out))
        types = [e["event_type"] for e in await events_for(pg.db, tenant_id)]
        assert types == ["runner.enrolled", "tenant.exported"]
