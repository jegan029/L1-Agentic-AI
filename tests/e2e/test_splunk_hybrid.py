"""Demo mode with only Splunk live (`LIVE_ADAPTERS=splunk`), from the webhook to the record.

The real service is assembled as `python -m src.l1_agent.main` assembles it.
Every connector is a mock except Splunk, which is the real connector over
its REST backend, talking to a replay server whose answers were written by
hand from Splunk's documentation. The same path against a real Splunk is in
tests/live/test_splunk_live.py.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, Dict, Tuple

import pytest

from src.l1_agent.config.settings import AgentSettings, LLMSettings, Settings, SplunkSettings
from src.l1_agent.demo import DEMO_INCIDENTS
from src.l1_agent.main import L1AgentService
from tests.api.support import client, token_headers
from tests.contract.replay import Replay, load

# What the service asks Splunk at start-up, before any incident: is it there?
HEALTH_ONLY = load("splunk/e2e_autosys.json")["exchanges"][:1]
AUTOSYS_INCIDENT = next(i for i in DEMO_INCIDENTS if i["number"] == "INC0009003")
SEARCH_STEP = "step-4"


def hybrid_settings(tmp_path, splunk: SplunkSettings) -> Settings:
    settings = Settings(
        agent=AgentSettings(demo_mode=True, live_adapters=("splunk",)),
        llm=LLMSettings(enabled=False),
        splunk=splunk,
    )
    url = f"sqlite+aiosqlite:///{(tmp_path / 'hybrid.db').as_posix()}"
    return replace(settings, database=replace(settings.database, url=url, tenant_slug="demo"))


async def run_incident(settings: Settings, incident: Dict[str, Any]) -> Tuple[Dict[str, Any], list]:
    """Post the incident to the webhook, let a worker run it, and read the record back."""
    service = L1AgentService(settings)
    try:
        ctx = await service.setup()
        from src.l1_agent.api.app import create_app

        async with client(create_app(ctx)) as http:
            accepted = await http.post("/webhook/incident", json=incident)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["status"] == "accepted"
            while await ctx.runs.work_once([ctx.tenant_id]):
                pass
            record = await http.get(
                f"/api/incidents/{incident['number']}",
                headers=await token_headers(ctx, "incidents.read"),
            )
        assert record.status_code == 200, record.text
        return record.json(), list(service._snow_client.work_notes)
    finally:
        await service.close()


def step(record: Dict[str, Any], step_id: str) -> Dict[str, Any]:
    return next(s for s in record["step_results"] if s["step_id"] == step_id)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("SPLUNK_TOKEN", "sp-hybrid")


class TestAutosysIncidentWithSplunkLive:
    async def test_the_search_step_returns_splunk_s_events_and_the_sop_decides(
        self, tmp_path, token
    ):
        async with Replay.from_file("splunk/e2e_autosys.json") as splunk:
            record, notes = await run_incident(
                hybrid_settings(
                    tmp_path,
                    SplunkSettings(base_url=splunk.url, allowed_indexes=("batch_logs", "app_logs")),
                ),
                AUTOSYS_INCIDENT,
            )
            splunk.assert_complete()

        # The SPLUNK_SEARCH step holds the events Splunk returned, not the mock's.
        search = step(record, SEARCH_STEP)
        assert search["step_type"] == "SPLUNK_SEARCH" and search["status"] == "success"
        assert search["tool_called"] == "Splunk"
        assert "Splunk search returned 2 result(s)" in search["evidence_snippet"]
        assert "exit_code=1" in search["evidence_snippet"]
        assert "PaymentService" not in search["evidence_snippet"]  # the mock's canned line
        assert search["audit"] == {
            "backend": "rest",
            "spl": 'search index=batch_logs source=autosys "BATCH_PAYMENT_PROCESS" '
            "(FAILURE OR ERROR) | head 100",
            "earliest": "-4h",
            "latest": "now",
            "time_range_clamped": False,
            "result_count": 2,
            "duration_ms": search["audit"]["duration_ms"],
            "outcome": "ok",
        }

        # The work note for the step carries the evidence.
        note = next(n["note"] for n in notes if f"Step {SEARCH_STEP} " in n["note"])
        assert "SUCCESS" in note and "exit_code=1" in note and "Connection refused" in note

        # The outcome is the SOP's: it verified the job, found it still failed, and said why.
        assert record["sop_id"] == "SOP-AUTOSYS-001"
        assert record["outcome"] == "escalated"
        assert "still in a failed state" in record["escalation_reason"]
        assert record["step_results"][-1]["step_id"] == "step-still-failed"
        assert step(record, "step-verify")["status"] == "fail"

    async def test_a_healthy_splunk_finds_nothing_and_the_run_still_escalates(
        self, tmp_path, token
    ):
        """No matching events is a successful search with no evidence, never a resolution."""
        exchanges = copy.deepcopy(load("splunk/e2e_autosys.json")["exchanges"])
        exchanges[-1]["response"]["json"]["results"] = []
        async with Replay(exchanges) as splunk:
            record, notes = await run_incident(
                hybrid_settings(
                    tmp_path, SplunkSettings(base_url=splunk.url, allowed_indexes=("batch_logs",))
                ),
                AUTOSYS_INCIDENT,
            )
            splunk.assert_complete()
        search = step(record, SEARCH_STEP)
        assert search["status"] == "success" and search["audit"]["result_count"] == 0
        assert search["evidence_snippet"] == "Splunk search returned 0 results."
        assert record["outcome"] == "escalated" and record["verified_by"] == ""
        assert not any("exit_code=1" in n["note"] for n in notes)

    async def test_a_search_the_guard_refuses_escalates_before_splunk_is_asked(
        self, tmp_path, token
    ):
        # The SOP searches batch_logs. Here only app_logs is allowed.
        async with Replay(HEALTH_ONLY) as splunk:
            record, notes = await run_incident(
                hybrid_settings(
                    tmp_path, SplunkSettings(base_url=splunk.url, allowed_indexes=("app_logs",))
                ),
                AUTOSYS_INCIDENT,
            )
            splunk.assert_complete()
            # The start-up health check, and no search.
            assert [r["path"] for r in splunk.requests] == ["/services/server/info"]
        search = step(record, SEARCH_STEP)
        assert search["status"] == "escalated"
        assert search["audit"]["outcome"] == "rejected" and search["audit"]["spl"] == ""
        assert record["outcome"] == "escalated"
        assert "index 'batch_logs' is not on the allow list" in record["escalation_reason"]
        # The run stopped at the search: nothing after it was called.
        assert record["step_results"][-1]["step_id"] == SEARCH_STEP
        assert any("Search refused by the guard" in n["note"] for n in notes)

    async def test_without_live_adapters_the_demo_is_unchanged(self, tmp_path):
        settings = hybrid_settings(tmp_path, SplunkSettings())
        settings = replace(settings, agent=replace(settings.agent, live_adapters=()))
        record, _ = await run_incident(settings, AUTOSYS_INCIDENT)
        search = step(record, SEARCH_STEP)
        assert search["tool_called"] == "MockSplunk" and search["audit"] == {}
        assert record["outcome"] == "escalated"
