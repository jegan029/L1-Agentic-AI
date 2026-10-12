"""The model command, and what demo mode sets up for model use (ADR 0009)."""

from __future__ import annotations

import json

import pytest

from src.l1_agent import cli
from src.l1_agent.ai import policy as model_policy
from src.l1_agent.ai.gateway import ModelGateway
from src.l1_agent.ai.providers import DeterministicProvider
from src.l1_agent.audit.log import events_for
from src.l1_agent.config.settings import (
    AgentSettings,
    ConfigError,
    DatabaseSettings,
    LLMSettings,
    Settings,
)
from src.l1_agent.demo import DEMO_INCIDENTS, load_sample_sops, seed_demo_model_policy
from src.l1_agent.models.incident import Incident

RULES = {"providers": ["deterministic"], "tasks": ["rank"], "regions": ["local"]}


async def run(settings: Settings, *argv: str) -> int:
    return await cli._run(cli._parser().parse_args(list(argv)), settings)


@pytest.fixture
async def env(tmp_path):
    from src.l1_agent.db.engine import Database

    url = f"sqlite+aiosqlite:///{(tmp_path / 'cli.db').as_posix()}"
    settings = Settings(
        agent=AgentSettings(demo_mode=True),
        llm=LLMSettings(enabled=False),
        database=DatabaseSettings(url=url, tenant_slug="demo"),
    )
    db = Database.create(url, demo_mode=True)
    await db.create_schema_for_demo()
    tenant_id = await db.ensure_tenant("demo")
    yield settings, db, tenant_id
    await db.close()


class TestModelCommand:
    async def test_show_set_and_usage(self, env, tmp_path, capsys):
        settings, db, tenant_id = env
        assert await run(settings, "model", "show") == 0
        assert json.loads(capsys.readouterr().out) == {"enabled": False, "rules": {}}

        rules = tmp_path / "model.json"
        rules.write_text(json.dumps(RULES), "utf-8")
        assert await run(settings, "model", "set", "--on", "--rules", str(rules)) == 0
        assert json.loads(capsys.readouterr().out) == {"enabled": True, "rules": RULES}

        assert await run(settings, "model", "set", "--off") == 0
        assert json.loads(capsys.readouterr().out) == {"enabled": False, "rules": RULES}

        assert await run(settings, "model", "usage") == 0
        assert json.loads(capsys.readouterr().out)["calls"] == 0

        events = [e for e in await events_for(db, tenant_id) if e["event_type"].startswith("model")]
        assert [e["event_type"] for e in events] == ["model.policy_changed"] * 2
        assert events[0]["actor"] == {"type": "system", "id": "cli"}

    async def test_an_invalid_policy_is_refused_with_its_problems(self, env, tmp_path):
        settings, db, tenant_id = env
        rules = tmp_path / "model.json"
        rules.write_text(json.dumps({"providers": ["bedrock"], "tasks": ["rank"]}), "utf-8")
        with pytest.raises(ConfigError, match="'bedrock' is not one of"):
            await run(settings, "model", "set", "--on", "--rules", str(rules))
        assert (await model_policy.get_model_policy(db, tenant_id)).enabled is False

    async def test_switching_on_with_no_rules_is_refused(self, env):
        settings, _, _ = env
        with pytest.raises(ConfigError, match="providers"):
            await run(settings, "model", "set", "--on")

    async def test_set_needs_something_to_set(self, env):
        settings, _, _ = env
        with pytest.raises(ConfigError, match="needs --on, --off, --rules"):
            await run(settings, "model", "set")

    def test_on_and_off_cannot_be_given_together(self):
        with pytest.raises(SystemExit):
            cli._parser().parse_args(["model", "set", "--on", "--off"])


class TestDemoMode:
    async def test_the_demo_tenant_may_use_the_stand_in_for_every_task(self, env):
        _, db, tenant_id = env
        await seed_demo_model_policy(db, tenant_id)
        policy = await model_policy.get_model_policy(db, tenant_id)
        assert policy.enabled and policy.providers == ("deterministic",)
        assert set(policy.tasks) == {"rank", "extract", "interpret", "draft"}
        sops = load_sample_sops()
        incident = Incident.from_servicenow(DEMO_INCIDENTS[0])
        advisor = ModelGateway(db, DeterministicProvider()).advisor(tenant_id, None, "INC1")
        ranking = await advisor.rank(incident, sops)
        assert ranking is not None and ranking.sop is sops[0], "the stand-in was asked and answered"
        assert (await model_policy.usage(db, tenant_id))["by_outcome"] == {"ok": 1}

    async def test_a_policy_someone_has_set_is_left_alone(self, env):
        _, db, tenant_id = env
        await seed_demo_model_policy(db, tenant_id)
        await model_policy.set_model_policy(db, tenant_id, cli.CLI, enabled=False)
        await seed_demo_model_policy(db, tenant_id)
        assert (await model_policy.get_model_policy(db, tenant_id)).enabled is False

    async def test_a_tenant_outside_demo_setup_starts_with_no_model(self, env):
        _, db, _ = env
        other = await db.ensure_tenant("another")
        assert (await model_policy.get_model_policy(db, other)).enabled is False

    async def test_the_demo_runs_end_to_end_with_model_requests_recorded(self, capsys):
        from src.l1_agent.demo import run_demo

        rows = await run_demo()
        out = capsys.readouterr().out
        assert "Model requests (deterministic stand-in, each one audited):" in out
        assert "hash chain verified" in out
        outcomes = [row["outcome"] for row in rows]
        assert outcomes.count("resolved") == 3 and outcomes.count("escalated") == 5
