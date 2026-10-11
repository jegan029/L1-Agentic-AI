"""The policy and kill switch commands, the ACTION step in the validator, and demo mode."""

from __future__ import annotations

import json

import pytest

from src.l1_agent import cli
from src.l1_agent.actions import store as action_store
from src.l1_agent.audit.log import events_for
from src.l1_agent.config.settings import (
    AgentSettings,
    ConfigError,
    DatabaseSettings,
    LLMSettings,
    Settings,
)
from src.l1_agent.models.sop import SOP
from src.l1_agent.sop import store as sop_store
from src.l1_agent.sop.validator import validate_sop
from tests.actions_support import restart_content


def demo_settings(tmp_path) -> Settings:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'cli.db').as_posix()}"
    return Settings(
        agent=AgentSettings(demo_mode=True),
        llm=LLMSettings(enabled=False),
        database=DatabaseSettings(url=url, tenant_slug="demo"),
    )


async def run(settings: Settings, *argv: str) -> int:
    return await cli._run(cli._parser().parse_args(list(argv)), settings)


@pytest.fixture
async def env(tmp_path):
    from src.l1_agent.db.engine import Database

    settings = demo_settings(tmp_path)
    db = Database.create(settings.database.url, demo_mode=True)
    await db.create_schema_for_demo()
    tenant_id = await db.ensure_tenant("demo")
    yield settings, db, tenant_id
    await db.close()


class TestPolicyCommands:
    async def test_policy_show_and_set(self, env, tmp_path, capsys):
        settings, db, tenant_id = env
        assert await run(settings, "policy", "show") == 0
        assert json.loads(capsys.readouterr().out)["autonomy"] == "shadow"

        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"rate": {"max_actions": 5, "per_minutes": 30}}), "utf-8")
        assert (
            await run(settings, "policy", "set", "--autonomy", "approval", "--rules", str(rules))
            == 0
        )
        shown = json.loads(capsys.readouterr().out)
        assert shown["autonomy"] == "approval" and shown["rules"]["rate"]["max_actions"] == 5
        assert shown["updated_by"] == "system:cli"
        types = [e["event_type"] for e in await events_for(db, tenant_id)]
        assert types.count("policy.changed") == 1

    async def test_invalid_rules_are_refused_and_nothing_changes(self, env, tmp_path):
        settings, db, tenant_id = env
        rules = tmp_path / "rules.json"
        rules.write_text(
            json.dumps({"automatic": [{"capability": "x", "targets": ["*"]}]}), "utf-8"
        )
        with pytest.raises(ConfigError, match="sop_id is required"):
            await run(settings, "policy", "set", "--rules", str(rules))
        with pytest.raises(ConfigError, match="needs --autonomy, --rules, or both"):
            await run(settings, "policy", "set")
        assert (await action_store.get_policy(db, tenant_id)).updated_at is None

    def test_an_unknown_autonomy_level_is_not_accepted(self):
        with pytest.raises(SystemExit):
            cli._parser().parse_args(["policy", "set", "--autonomy", "full"])

    async def test_kill_switch_on_off_and_status(self, env, capsys):
        settings, db, tenant_id = env
        assert await run(settings, "killswitch", "status") == 0
        assert "tenant kill switch: off" in capsys.readouterr().out
        assert await run(settings, "killswitch", "on", "--reason", "runaway restarts") == 0
        assert "tenant kill switch: ON" in capsys.readouterr().out
        policy = await action_store.get_policy(db, tenant_id)
        assert policy.kill_switch and policy.kill_reason == "runaway restarts"
        assert await run(settings, "killswitch", "off") == 0
        assert not (await action_store.get_policy(db, tenant_id)).kill_switch
        types = [e["event_type"] for e in await events_for(db, tenant_id)]
        assert types == ["killswitch.engaged", "killswitch.released"]

    async def test_the_global_switch_needs_the_migration_role(self, env):
        settings, _, _ = env
        with pytest.raises(ConfigError, match="DATABASE_MIGRATION_URL"):
            await run(settings, "killswitch", "on", "--global")

    async def test_the_global_switch_is_set_with_the_migration_role(self, pg, pg_urls, capsys):
        tenant_id = await pg.new_tenant("cli")
        settings = Settings(
            agent=AgentSettings(demo_mode=False, webhook_secret="s" * 40),
            llm=LLMSettings(enabled=False),
            database=DatabaseSettings(url=pg_urls.app, migration_url=pg_urls.owner),
        )
        try:
            assert await run(settings, "killswitch", "on", "--global", "--reason", "drill") == 0
            assert "global kill switch on" in capsys.readouterr().out
            state = await action_store.global_kill_switch(pg.db)
            assert state["on"] is True and state["set_by"] == "system:cli"
            assert await action_store.switches_on(pg.db, tenant_id) == ("global",)
        finally:
            assert await run(settings, "killswitch", "off", "--global") == 0
        assert (await action_store.global_kill_switch(pg.db))["on"] is False
        events = await events_for(pg.db, tenant_id)
        assert [(e["event_type"], e["payload"]["scope"]) for e in events] == [
            ("killswitch.engaged", "global"),
            ("killswitch.released", "global"),
        ]


def sop_with(**step_changes) -> SOP:
    content = restart_content()
    top = {k: step_changes.pop(k) for k in list(step_changes) if k == "change_template"}
    for step in content["steps"]:
        if step["step_id"] == "step-restart":
            step.update(step_changes)
    content.update(top)
    return SOP.from_dict(content)


class TestActionStepValidation:
    def test_the_sample_restart_sop_is_valid(self):
        assert validate_sop(SOP.from_dict(restart_content())) == []

    @pytest.mark.parametrize(
        "change, expected",
        [
            (dict(capability=""), "unknown capability ''"),
            (dict(capability="autosys.job.delete"), "unknown capability"),
            (dict(capability="autosys.job.status"), "is a read; use a CHECK step"),
            (dict(capability="AUTOSYS_STATUS"), "unknown capability"),
            (dict(capability="servicenow.incident.update"), "unknown capability"),
            (dict(parameters={}), "parameter 'job_name' is required"),
            (dict(parameters={"job_name": "X", "force": True}), "'force' is not allowed"),
            (dict(parameters={"job_name": "X; rm -rf /"}), "characters that are not allowed"),
            (dict(parameters={"job_name": "${undeclared}"}), "no parameter declares it"),
            (dict(verify={"assert": "result.jobs[0].status =="}), "verify assert"),
            (dict(verify={"assert": "incident.priority == 1"}), "unknown name 'incident'"),
            (dict(verify={"retries": 61}), "verify retries"),
            (dict(verify={"wait_seconds": 301}), "verify wait_seconds"),
            (dict(verify={"check": "splunk.search"}), "unknown verify keys"),
            (dict(on_success=""), "on_success is required"),
            (dict(change_template=""), "must name a change_template"),
        ],
    )
    def test_a_bad_action_step_is_rejected(self, change, expected):
        problems = validate_sop(sop_with(**change))
        assert any(expected in p for p in problems), problems

    def test_a_check_step_cannot_name_a_write(self):
        content = restart_content()
        content["steps"][0]["capability"] = "autosys.job.force_start"
        content["steps"][0]["parameters"] = {"job_name": "${job_name}"}
        problems = validate_sop(SOP.from_dict(content))
        assert any("is a write and cannot be used as a check" in p for p in problems)

    def test_a_verify_step_cannot_use_a_write_as_its_check(self):
        content = restart_content()
        verify = next(s for s in content["steps"] if s["step_id"] == "step-verify")
        verify["parameters"]["check"] = "autosys.job.force_start"
        verify["parameters"]["parameters"] = {"job_name": "${job_name}"}
        problems = validate_sop(SOP.from_dict(content))
        assert any("is a write and cannot be used as a check" in p for p in problems)

    def test_an_action_is_not_a_verification(self):
        """An SOP that resolves straight after its action is rejected."""
        content = restart_content()
        for step in content["steps"]:
            if step["step_id"] == "step-restart":
                step["on_success"] = "step-resolve"
        problems = validate_sop(SOP.from_dict(content))
        assert any("RESOLVE can be reached without a passed VERIFY" in p for p in problems)

    def test_an_sop_without_actions_hashes_as_it_did_before(self):
        """Adding the new fields must not change the hash of what is already published."""
        from src.l1_agent.demo import sample_sop_contents

        for content in sample_sop_contents():
            normal = sop_store.normalise(content)
            has_action = any(s["step_type"] == "ACTION" for s in normal["steps"])
            assert ("change_template" in normal) == has_action
            assert all(("verify" in s) == bool(s.get("verify")) for s in normal["steps"])

    def test_the_reviewer_is_shown_the_write_and_its_tier(self):
        used = sop_store.capabilities_of(restart_content())
        write = next(c for c in used if c["step_id"] == "step-restart")
        assert write == {
            "step_id": "step-restart",
            "capability": "autosys.job.force_start",
            "known": True,
            "kind": "write",
            "tier": "T1",
            "connector": "Autosys",
            "preview": False,
        }


class TestDemo:
    async def test_the_demo_runs_every_route_and_its_audit_log_verifies(self, capsys):
        from src.l1_agent.demo import run_demo

        rows = await run_demo()
        out = capsys.readouterr().out
        outcomes = {r["incident"]: r["outcome"] for r in rows}
        assert outcomes == {
            "INC0012345": "escalated",
            "INC0009005": "resolved",
            "INC0009003": "escalated",
            "INC0009004": "escalated",
            "INC0009002": "escalated",
            "INC0009006": "resolved",  # restarted automatically (T1, on the automatic list)
            "INC0009007": "resolved",  # T2: waited for an approval
            "INC0009008": "escalated",  # restarted, failed again
        }
        assert "Waiting for approval: autosys.job.force_start" in out and "tier T2" in out
        assert "hash chain verified" in out
        assert "Change records opened in the mock ServiceNow: 3" in out

    async def test_a_demo_tenant_is_set_to_act_and_a_real_tenant_is_not(self, backend):
        from src.l1_agent.demo import seed_demo_policy, seed_demo_sops

        tenant = await backend.new_tenant("demo")
        assert (await action_store.get_policy(backend.db, tenant)).autonomy.value == "shadow"
        await seed_demo_sops(backend.db, tenant)
        await seed_demo_policy(backend.db, tenant)
        policy = await action_store.get_policy(backend.db, tenant)
        assert policy.autonomy.value == "automatic"
        await action_store.set_policy(backend.db, tenant, cli.CLI, autonomy="shadow")
        await seed_demo_policy(backend.db, tenant)  # starting again does not undo a change
        assert (await action_store.get_policy(backend.db, tenant)).autonomy.value == "shadow"
