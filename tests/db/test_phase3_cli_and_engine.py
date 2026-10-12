"""Command-line tools for SOPs and connections, and capability steps in the engine."""

from __future__ import annotations

import json
import uuid

import pytest

from src.l1_agent import cli
from src.l1_agent.adapters.mock_adapters import MockDynatraceAdapter, MockSplunkAdapter
from src.l1_agent.audit.log import events_for
from src.l1_agent.config.settings import (
    AgentSettings,
    ConfigError,
    DatabaseSettings,
    LLMSettings,
    Settings,
)
from src.l1_agent.connectors import hub as connections
from src.l1_agent.demo import sample_sop_contents
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.models.evidence import ExecutionOutcome, StepStatus
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.sop import store
from src.l1_agent.sop.validator import validate_sop

MQ = next(c for c in sample_sop_contents() if c["sop_id"] == "SOP-MQ-001")


def demo_settings(tmp_path) -> Settings:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'cli.db').as_posix()}"
    return Settings(
        agent=AgentSettings(demo_mode=True),
        llm=LLMSettings(enabled=False),
        database=DatabaseSettings(url=url, tenant_slug="demo"),
    )


async def run(settings: Settings, *argv: str) -> int:
    return await cli._run(cli._parser().parse_args(list(argv)), settings)


async def with_service(settings: Settings, *argv: str) -> int:
    return await cli._with_service(cli._parser().parse_args(list(argv)), settings)


@pytest.fixture
async def env(tmp_path):
    """A demo database with a tenant, the way `serve` leaves it."""
    from src.l1_agent.db.engine import Database

    settings = demo_settings(tmp_path)
    db = Database.create(settings.database.url, demo_mode=True)
    await db.create_schema_for_demo()
    tenant_id = await db.ensure_tenant("demo")
    yield settings, db, tenant_id
    await db.close()


class TestSopCommands:
    async def test_import_creates_a_draft_and_nothing_more(self, env, tmp_path, capsys):
        settings, db, tenant_id = env
        path = tmp_path / "sop.json"
        path.write_text(json.dumps(MQ), encoding="utf-8")
        assert await run(settings, "sop", "import", "--file", str(path)) == 0
        out = capsys.readouterr().out
        assert "draft " in out and "different person must approve" in out

        [version] = await store.list_versions(db, tenant_id)
        assert version["status"] == "draft" and version["author"] == "system:cli"
        assert await store.published(db, tenant_id) == [], "the command line cannot publish"
        assert await run(settings, "sop", "list") == 0
        assert "SOP-MQ-001\t-\tdraft" in capsys.readouterr().out

    async def test_importing_something_that_is_not_an_sop_is_an_error(self, env, tmp_path):
        settings, _, _ = env
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"title": "no id"}), encoding="utf-8")
        with pytest.raises(ConfigError, match="sop_id is required"):
            await run(settings, "sop", "import", "--file", str(path))

    def test_there_is_no_command_that_approves_or_publishes(self):
        actions = cli._parser()._subparsers._group_actions[0].choices["sop"]
        names = set(actions._subparsers._group_actions[0].choices)
        assert names == {"import", "list"}


class TestConnectionCommands:
    async def test_set_list_and_remove(self, env, tmp_path, capsys):
        settings, db, tenant_id = env
        config = tmp_path / "splunk.json"
        config.write_text(json.dumps({"base_url": "https://splunk.example"}), encoding="utf-8")
        code = await run(
            settings, "connection", "set", "--connector", "splunk", "--config", str(config),
            "--secret", "token=vault:splunk/token",
        )  # fmt: skip
        assert code == 0
        assert await run(settings, "connection", "list") == 0
        assert "splunk\tenabled\ttoken=vault:splunk/token" in capsys.readouterr().out

        stored = await connections.stored_connections(db, tenant_id)
        assert stored["splunk"].config == {"base_url": "https://splunk.example"}
        assert await run(settings, "connection", "remove", "--connector", "splunk") == 0
        assert await run(settings, "connection", "remove", "--connector", "splunk") == 1
        types = [e["event_type"] for e in await events_for(db, tenant_id)]
        assert types == ["connection.updated", "connection.removed"]

    async def test_a_credential_value_on_the_command_line_is_refused(self, env, tmp_path):
        settings, db, tenant_id = env
        config = tmp_path / "splunk.json"
        config.write_text(json.dumps({"base_url": "https://splunk.example"}), encoding="utf-8")
        for secret in ("token=the-real-token", "token", "=vault:x/y"):
            with pytest.raises(ConfigError):
                await run(
                    settings, "connection", "set", "--connector", "splunk",
                    "--config", str(config), "--secret", secret,
                )  # fmt: skip
        assert await connections.stored_connections(db, tenant_id) == {}

    def test_connectors_lists_every_capability_with_its_tier(self, capsys):
        assert cli.main(["connectors"]) == 0
        out = capsys.readouterr().out
        assert "splunk.search\tread\tT0" in out and "(preview)" in out
        # The one write, with its tier (Phase 4).
        assert [
            line.split("\t")[0].strip() for line in out.splitlines() if "\twrite\t" in line
        ] == ["autosys.job.force_start"]
        assert "autosys.job.force_start\twrite\tT1" in out

    async def test_connection_test_and_knowledge_sync_in_demo_mode(self, tmp_path, capsys):
        settings = demo_settings(tmp_path)
        assert await with_service(settings, "connection", "test", "--connector", "splunk") == 0
        assert "ok   reach the target" in capsys.readouterr().out
        with pytest.raises(ConfigError, match="Unknown connector"):
            await with_service(settings, "connection", "test", "--connector", "teleport")
        assert await with_service(settings, "knowledge", "sync") == 0
        assert "no knowledge source is configured" in capsys.readouterr().out


def capability_sop(check: dict, verify_check: str) -> SOP:
    return SOP.from_dict(
        {
            "sop_id": "SOP-CAP",
            "title": "Capability steps",
            "steps": [
                {**check, "step_id": "look", "on_success": "verify", "on_failure": "stop"},
                {
                    "step_id": "verify",
                    "step_type": "VERIFY",
                    "parameters": {
                        "check": verify_check,
                        "parameters": {"query": "index=app ERROR"},
                        "assert": "result.result_count >= 0",
                    },
                    "on_success": "done",
                    "on_failure": "stop",
                },
                {"step_id": "done", "step_type": "RESOLVE", "parameters": {"text": "ok"}},
                {"step_id": "stop", "step_type": "ESCALATE", "parameters": {"reason": "no"}},
            ],
        }
    )


CHECK = {
    "step_type": "CHECK",
    "capability": "splunk.search",
    "parameters": {"query": "index=app ERROR"},
}
INCIDENT = Incident(sys_id=uuid.uuid4().hex, number="INC0000042", short_description="x")


class TestCapabilitySteps:
    async def test_a_check_step_names_a_capability_and_runs_through_its_connector(self):
        sop = capability_sop(CHECK, "splunk.search")
        assert validate_sop(sop) == []
        executor = SOPExecutor(adapters={"splunk": MockSplunkAdapter()}, retry_max_attempts=1)
        summary = await executor.execute_sop(INCIDENT, sop)
        assert summary.outcome == ExecutionOutcome.RESOLVED and summary.verified_by == "verify"
        assert [r.tool_called for r in summary.step_results[:2]] == ["MockSplunk", "MockSplunk"]

    @pytest.mark.parametrize(
        "check, problem",
        [
            ({**CHECK, "capability": "splunk.delete"}, "unknown capability 'splunk.delete'"),
            ({**CHECK, "capability": ""}, "unknown capability ''"),
            ({**CHECK, "capability": "SPLUNK.SEARCH"}, "unknown capability"),
            (
                {**CHECK, "capability": "servicenow_kb.articles.list", "parameters": {}},
                "cannot be used in an SOP step",
            ),
            (
                {**CHECK, "parameters": {"query": "x", "exec_mode": "oneshot"}},
                "parameter 'exec_mode' is not allowed for splunk.search",
            ),
            (
                {"step_type": "SPLUNK_SEARCH", "capability": "splunk.search",
                 "parameters": {"query": "x"}},
                "only a CHECK or ACTION step names a capability",
            ),
        ],
    )  # fmt: skip
    def test_the_validator_rejects_a_bad_capability_step(self, check, problem):
        problems = validate_sop(capability_sop(check, "splunk.search"))
        assert any(problem in p for p in problems), problems

    def test_verify_must_name_a_known_read_check(self):
        problems = validate_sop(capability_sop(CHECK, "splunk.purge"))
        assert any("'check' must be an adapter step type" in p for p in problems)

    async def test_a_capability_whose_connector_is_missing_needs_a_human(self):
        sop = capability_sop(CHECK, "splunk.search")
        executor = SOPExecutor(adapters={"dynatrace": MockDynatraceAdapter()})
        summary = await executor.execute_sop(INCIDENT, sop)
        assert summary.outcome == ExecutionOutcome.NEEDS_HUMAN
        assert summary.step_results[0].status == StepStatus.ERROR
        assert "not configured" in summary.escalation_reason
