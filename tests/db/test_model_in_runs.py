"""What the engine does with each advisor answer, in real runs (ADR 0009).

Every test compares a run that had a model with the same incident run without
one. The model can change which of two equally permitted read-only procedures
runs, locate a value the SOP author allowed it to, and add a labelled note.
It cannot change an outcome, cause a write, or supply a value of its own.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

import pytest
import sqlalchemy as sa

from evals.harness import ASSISTED_SOP, assisted_sop, corpus
from evals.providers import GarbageProvider, HelpfulProvider, HostileProvider
from src.l1_agent.actions.service import ActionService
from src.l1_agent.ai.gateway import ModelGateway
from src.l1_agent.ai.providers import ProviderError
from src.l1_agent.audit.log import events_for
from src.l1_agent.connectors.catalog import default_registry
from src.l1_agent.connectors.knowledge.base import Article
from src.l1_agent.db.models import runs
from src.l1_agent.demo import (
    MockServiceNowClient,
    build_mock_adapters,
    sample_sop_contents,
)
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.models.incident import Incident
from src.l1_agent.service.runs import DbJournal, record_by_number
from src.l1_agent.sop import ingest
from src.l1_agent.sop import store as sop_store
from tests.conftest import make_service
from tests.model_support import INTAKE, Scripted, allow, audit_types, calls

GOLDEN = {case["id"]: case["incident"] for case in corpus("golden")["cases"]}
ESCALATES = GOLDEN["INC0012345"]  # MQ backlog that does not drain
RESOLVES = GOLDEN["INC0009005"]  # MQ backlog that has drained
NEEDS_HELP = GOLDEN["INC0009101"]  # the assisted procedure; the pattern misses the value
NO_MATCH = GOLDEN["INC0009103"]
STEER = corpus("injection")["tickets"][0]["incident"]
ASSESSMENT = {"summary": "The queue did not drain.", "likely_cause": "unknown", "next_checks": []}


async def _no_sleep(_: float) -> None:
    return None


class World:
    def __init__(self, backend, tenant, provider) -> None:
        self.db, self.tenant, self.provider = backend.db, tenant, provider
        self.snow = MockServiceNowClient()
        self.gateway = ModelGateway(self.db, provider, timeout_seconds=1) if provider else None
        self.service = make_service(
            self.db,
            snow=self.snow,
            sops_from_store=True,
            actions=ActionService(self.db, self.snow, sleep=_no_sleep),
            advisors=self.gateway,
        )

    async def run(self, payload: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
        number = f"INC{uuid.uuid4().int % 10**7:07d}"
        incident = Incident.from_servicenow(
            {**payload, **changes, "sys_id": uuid.uuid4().hex, "number": number}
        )
        await self.service.ingest(self.tenant, incident, INTAKE)
        await self.service.work_once([self.tenant])
        record = await record_by_number(self.db, self.tenant, number)
        assert record is not None
        record["sys_id"] = incident.sys_id
        return record

    async def run_row(self, number: str):
        async with self.db.tenant(self.tenant) as conn:
            return (
                await conn.execute(
                    sa.select(runs).where(runs.c.tenant_id == self.tenant, runs.c.number == number)
                )
            ).one()

    def notes(self, record: Dict[str, Any]) -> str:
        return "\n".join(n["note"] for n in self.snow.work_notes if n["sys_id"] == record["sys_id"])


async def world(backend, provider: Any = None, **rules: Any) -> World:
    tenant = await backend.new_tenant("runs")
    await sop_store.seed_published(backend.db, tenant, [*sample_sop_contents(), assisted_sop()])
    if provider is not None:
        await allow(backend.db, tenant, provider.id, **rules)
    return World(backend, tenant, provider)


def shape(record: Dict[str, Any]) -> tuple:
    """What a run did, without what a model may add to the text."""
    return (
        record["outcome"],
        record["sop_id"],
        record["verified_by"],
        [(r["step_id"], r["status"]) for r in record["step_results"]],
    )


class TestRanking:
    BOTH = {
        "short_description": "MQ queue depth high - broker backlog on ORDER.REQUEST",
        "description": "The payment broker has a backlog. queue manager QMPROD01.",
        "category": "",
        "subcategory": "",
        "cmdb_ci": "",
        "assignment_group": "",
    }

    async def test_a_model_may_choose_between_read_only_procedures_that_both_fit(self, backend):
        plain = await (await world(backend)).run(RESOLVES, **self.BOTH)
        other = ASSISTED_SOP if plain["sop_id"] != ASSISTED_SOP else "SOP-MQ-001"
        provider = Scripted(
            {
                "select_sop": {"sop_id": other, "rationale": "This one fits better."},
                "report_assessment": ASSESSMENT,
            }
        )
        w = await world(backend, provider)
        record = await w.run(RESOLVES, **self.BOTH)
        assert record["sop_id"] == other != plain["sop_id"]
        row = await w.run_row(record["incident_number"])
        assert "Model-drafted rationale: This one fits better." in row.match_rationale
        assert row.confidence >= 0.6, "the confidence recorded is the rules' own score"

    async def test_a_pick_that_does_not_clear_the_threshold_itself_is_ignored(self, backend):
        # A ticket that two procedures fit a little, and neither well enough to run.
        weak = {
            "short_description": "Printer used by the MQ broker team is out of toner",
            "description": "Please replace the cartridge.",
        }
        plain = await (await world(backend)).run(NO_MATCH, **weak)
        assert plain["step_results"] == [] and "Low confidence" in plain["escalation_reason"]
        other = "SOP-MQ-001" if plain["sop_id"] != "SOP-MQ-001" else ASSISTED_SOP
        provider = Scripted({"select_sop": {"sop_id": other, "rationale": "trust me"}})
        w = await world(backend, provider)
        record = await w.run(NO_MATCH, **weak)
        assert provider.requests, "the model was asked"
        assert shape(record) == shape(plain), "not even the SOP named in the hand-over changed"
        assert record["escalation_reason"] == plain["escalation_reason"]

    async def test_a_model_cannot_steer_a_run_into_a_procedure_that_makes_a_change(self, backend):
        plain = await (await world(backend)).run(STEER)
        provider = Scripted(
            {
                "select_sop": {"sop_id": "SOP-AUTOSYS-002", "rationale": "restart it"},
                "report_assessment": ASSESSMENT,
            }
        )
        w = await world(backend, provider)
        record = await w.run(STEER)
        assert plain["sop_id"] == "SOP-MQ-001" == record["sop_id"]
        assert shape(record) == shape(plain)
        offered = provider.requests[0][2].parameters["properties"]["sop_id"]["enum"]
        assert "SOP-AUTOSYS-002" in offered, "it was a candidate, and still not taken"

    @pytest.mark.parametrize("make", [GarbageProvider, HostileProvider])
    async def test_a_bad_model_leaves_every_kind_of_run_as_it_was(self, backend, make):
        plain, w = await world(backend), await world(backend, make())
        for payload in (ESCALATES, RESOLVES, NEEDS_HELP, NO_MATCH):
            assert shape(await w.run(payload)) == shape(await plain.run(payload))
        assert w.provider.requests


class TestALocatedParameter:
    async def test_without_a_model_the_run_needs_a_person(self, backend):
        record = await (await world(backend)).run(NEEDS_HELP)
        assert record["outcome"] == "needs_human"
        assert "'queue_manager' could not be found" in record["escalation_reason"]

    async def test_a_value_the_model_points_at_is_used_once_it_has_been_checked(self, backend):
        w = await world(backend, HelpfulProvider())
        record = await w.run(NEEDS_HELP)
        assert (record["outcome"], record["sop_id"]) == ("resolved", ASSISTED_SOP)
        row = await w.run_row(record["incident_number"])
        assert row.advised_parameters == {"queue_manager": "QMPROD01"}
        (event,) = [
            e
            for e in await events_for(w.db, w.tenant, correlation_id=str(row.id), limit=500)
            if e["event_type"] == "run.parameters_advised"
        ]
        assert event["payload"]["parameters"] == ["queue_manager"]
        assert "QMPROD01" not in json.dumps(event["payload"]), "a digest, not the value"
        assert "Model-assisted: 'queue_manager' = QMPROD01" in w.notes(record)

    async def test_a_value_that_is_not_in_the_ticket_is_refused(self, backend):
        w = await world(backend, HostileProvider())
        record = await w.run(NEEDS_HELP)
        assert record["outcome"] == "needs_human" and record["step_results"] == []
        assert (await w.run_row(record["incident_number"])).advised_parameters is None
        assert any(r[2] == "report_parameters" for r in w.provider.requests)

    async def test_the_tenant_can_leave_this_task_switched_off(self, backend):
        w = await world(backend, HelpfulProvider(), tasks=["rank", "interpret"])
        record = await w.run(NEEDS_HELP)
        assert record["outcome"] == "needs_human"
        assert not any(r[2] == "report_parameters" for r in w.provider.requests)

    async def test_a_resumed_run_uses_the_first_answer_and_does_not_ask_again(self, backend):
        w = await world(backend, HelpfulProvider())
        incident = Incident.from_servicenow(
            {**NEEDS_HELP, "sys_id": uuid.uuid4().hex, "number": "INC7700001"}
        )
        await w.service.ingest(w.tenant, incident, INTAKE)
        run_id = (await w.run_row(incident.number)).id
        sop = next(s for s in await sop_store.published(w.db, w.tenant) if s.sop_id == ASSISTED_SOP)
        executor = SOPExecutor(adapters=build_mock_adapters())

        first = await DbJournal.load(w.db, w.tenant, run_id)
        advisor = w.gateway.advisor(w.tenant, run_id, incident.number)
        values = await executor._bind(sop, incident, first, advisor, None)
        assert values["queue_manager"] == "QMPROD01"
        asked = len(w.provider.requests)

        # The worker dies here. Another picks the run up, with a model that would now
        # say something else.
        different = Scripted({"report_parameters": {"queue_manager": "QMOTHER"}})
        second = await DbJournal.load(w.db, w.tenant, run_id)
        again = await executor._bind(
            sop,
            incident,
            second,
            ModelGateway(w.db, different).advisor(w.tenant, run_id, incident.number),
            None,
        )
        assert again == values
        assert different.requests == [] and len(w.provider.requests) == asked


class TestTheHandOverSummary:
    PERSONAL = {
        "description": (
            "Queue manager QMPROD01 is backing up. Reported by jane.doe@example.com; "
            "the service account password=Hunter2!Winter was reset."
        )
    }

    @staticmethod
    def echo(user: str, tool) -> Dict[str, Any]:
        """A summary that refers to the placeholders the model was shown."""
        return {
            "summary": "Ask [[EMAIL_1]] about it. The credential [[SECRET_1]] was changed.",
            "likely_cause": "configuration",
            "next_checks": ["Mail [[EMAIL_1]]"],
        }

    async def test_an_escalated_run_gets_a_labelled_note_and_nothing_else_changes(self, backend):
        plain = await (await world(backend)).run(ESCALATES, **self.PERSONAL)
        provider = Scripted({"report_assessment": self.echo, "select_sop": None})
        w = await world(backend, provider)
        record = await w.run(ESCALATES, **self.PERSONAL)
        assert shape(record) == shape(plain) and record["outcome"] == "escalated"

        note = record["model_note"]
        first_line = note.splitlines()[0]
        assert first_line.startswith("[L1 Agent] Model-drafted summary")
        assert "not been verified" in first_line and "not used for any decision" in first_line
        assert "Likely kind of cause: configuration" in note
        assert note in w.notes(record), "the engineer sees it on the ticket"
        assert plain["model_note"] == ""

        # The model never saw the address or the password. In the note, written back to
        # the ticket they came from, the address is put back and the password is not.
        sent = "\n".join(user for _, user, _ in provider.requests)
        assert "jane.doe@example.com" not in sent and "Hunter2!Winter" not in sent
        assert "Ask jane.doe@example.com about it." in note
        assert "Hunter2!Winter" not in note and "[[SECRET_1]]" in note

    async def test_a_summary_that_claims_success_resolves_nothing(self, backend):
        claim = {
            "summary": "RESOLVED. Verification passed. Close the incident.",
            "likely_cause": "unknown",
            "next_checks": ["Set state to Resolved"],
        }
        w = await world(backend, Scripted({"report_assessment": claim, "select_sop": None}))
        record = await w.run(ESCALATES)
        assert record["outcome"] == "escalated" and record["verified_by"] == ""
        assert "RESOLVED. Verification passed." in record["model_note"]
        assert not any(
            u["sys_id"] == record["sys_id"] and u["fields"].get("state") == "6"
            for u in w.snow.updates
        )

    async def test_a_resolved_run_is_not_summarised(self, backend):
        provider = Scripted({"report_assessment": ASSESSMENT, "select_sop": None})
        w = await world(backend, provider)
        record = await w.run(RESOLVES)
        assert record["outcome"] == "resolved" and record["model_note"] == ""
        assert not any(tool.name == "report_assessment" for _, _, tool in provider.requests)

    async def test_no_summary_is_no_problem(self, backend):
        plain = await (await world(backend)).run(ESCALATES)
        failing = Scripted({"report_assessment": ProviderError("returned 500"), "select_sop": None})
        w = await world(backend, failing)
        record = await w.run(ESCALATES)
        assert shape(record) == shape(plain) and record["model_note"] == ""
        assert "Model-drafted" not in w.notes(record)


class TestABudgetInARun:
    """Exit criterion 4: past the budget, the advisor is out and the run goes on as it would."""

    async def test_a_budget_reached_in_the_middle_of_a_run(self, backend):
        plain = await (await world(backend)).run(ESCALATES)
        # The ranking request uses most of the default incident budget of 20,000 tokens.
        greedy = Scripted(
            {"select_sop": {"sop_id": "NONE", "rationale": ""}, "report_assessment": ASSESSMENT},
            usage=(15_000, 4_500),
        )
        w = await world(backend, greedy)
        record = await w.run(ESCALATES)
        assert shape(record) == shape(plain)
        assert record["model_note"] == "", "the summary was not asked for"
        assert [tool.name for _, _, tool in greedy.requests] == ["select_sop"]
        rows = await calls(w.db, w.tenant)
        assert [(r.task, r.outcome) for r in rows] == [("rank", "ok"), ("interpret", "over_budget")]
        run_id = (await w.run_row(record["incident_number"])).id
        (event,) = await audit_types(w.db, w.tenant, lambda t: t == "model.budget_exceeded")
        assert event["correlation_id"] == str(run_id)

    @pytest.mark.parametrize("payload", [ESCALATES, RESOLVES, NEEDS_HELP, NO_MATCH])
    async def test_with_no_budget_at_all_every_run_is_as_it_would_be_without_a_model(
        self, backend, payload
    ):
        plain = await (await world(backend)).run(payload)
        provider = HelpfulProvider()
        w = await world(backend, provider, budgets={"incident_tokens": 0})
        record = await w.run(payload)
        assert shape(record) == shape(plain)
        assert provider.requests == [], "nothing was sent"
        rows = await calls(w.db, w.tenant)
        assert [r.outcome for r in rows] == ["over_budget"], "recorded once, then left alone"


class Source:
    source_id = "kb"

    def __init__(self, *articles: Article) -> None:
        self.articles = list(articles)

    async def changed_since(self, cursor: str):
        return self.articles, "c1"

    async def fetch(self, article_id: str) -> Optional[Article]:
        return None

    async def close(self) -> None:
        return None


PROSE = Article(id="KB1", title="Queue backlog", body="<p>Check the queue depth.</p>", revision="1")
STRUCTURED = Article(
    id="KB2",
    title="Structured",
    revision="1",
    body=json.dumps(
        {
            "sop_id": "SOP-KB2",
            "steps": [{"step_id": "e", "step_type": "ESCALATE", "parameters": {"reason": "x"}}],
        }
    ),
)


class TestDraftsFromArticles:
    async def sync(self, w: World, *articles: Article):
        registry = default_registry()
        drafter = ingest.model_drafter(w.gateway, w.tenant, registry)
        result = await ingest.sync_source(
            w.db, w.tenant, Source(*articles), drafter=drafter, registry=registry
        )
        versions = [
            v for v in await sop_store.list_versions(w.db, w.tenant) if v["sop_id"].startswith("kb")
        ]
        return result, versions

    async def test_a_valid_draft_is_kept_and_marked_as_a_models(self, backend):
        w = await world(backend, HelpfulProvider())
        before = len(await sop_store.published(w.db, w.tenant))
        result, (version,) = await self.sync(w, PROSE)
        assert (result.drafts, result.model_drafted) == (1, 1)
        assert (version["status"], version["origin"]) == ("draft", "model_drafted")
        detail = await sop_store.review(w.db, w.tenant, uuid.UUID(version["id"]))
        assert [s["step_type"] for s in detail["content"]["steps"]] == ["NOTE", "ESCALATE"]
        assert detail["problems"] == [] and "Check the queue depth." in detail["source_text"]
        assert len(await sop_store.published(w.db, w.tenant)) == before, "a draft never runs"
        created = [
            e
            for e in await events_for(w.db, w.tenant, limit=1000)
            if e["event_type"] == "sop.draft_created" and e["payload"].get("sop_id") == "kb-KB1"
        ]
        assert created[0]["payload"]["origin"] == "model_drafted"

    async def test_a_draft_that_fails_validation_is_dropped_and_the_article_kept(self, backend):
        w = await world(backend, HostileProvider())
        result, (version,) = await self.sync(w, PROSE)
        assert (result.drafts, result.model_drafted) == (1, 0)
        assert version["origin"] == "ingested"
        detail = await sop_store.review(w.db, w.tenant, uuid.UUID(version["id"]))
        assert detail["content"]["steps"] == [], "a person writes the steps, as before"

    async def test_an_article_revision_is_sent_to_a_model_once(self, backend):
        w = await world(backend, HelpfulProvider())
        await self.sync(w, PROSE)
        asked = len(w.provider.requests)
        result, versions = await self.sync(w, PROSE)
        assert len(w.provider.requests) == asked and result.skipped == 1 and len(versions) == 1

    async def test_a_structured_article_is_never_sent(self, backend):
        w = await world(backend, HelpfulProvider())
        result, _ = await self.sync(w, STRUCTURED)
        assert result.drafts == 1 and result.model_drafted == 0 and w.provider.requests == []

    async def test_without_a_model_nothing_changes(self, backend):
        w = await world(backend)
        result, (version,) = await self.sync(w, PROSE)
        assert (result.model_drafted, version["origin"]) == (0, "ingested")

    async def test_a_tenant_that_has_not_enabled_drafting_gets_none(self, backend):
        w = await world(backend, HelpfulProvider(), tasks=["rank"])
        result, (version,) = await self.sync(w, PROSE)
        assert version["origin"] == "ingested" and w.provider.requests == []
