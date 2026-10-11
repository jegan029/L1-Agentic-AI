"""Knowledge ingestion, per-tenant connections, and claiming a ticket before working it."""

from __future__ import annotations

import json
import uuid
from typing import List, Optional, Tuple

import pytest
import sqlalchemy as sa

from src.l1_agent.audit.log import Actor, events_for
from src.l1_agent.config.settings import (
    ConfigError,
    DynatraceSettings,
    SecretsSettings,
    Settings,
    SplunkSettings,
    WindowsShareSettings,
)
from src.l1_agent.connectors import hub as connections
from src.l1_agent.connectors.hub import ConnectorHub, environment_connections
from src.l1_agent.connectors.itsm.base import ClaimResult
from src.l1_agent.connectors.knowledge.base import Article
from src.l1_agent.connectors.sdk.base import StepContext
from src.l1_agent.connectors.sdk.secrets import SecretResolver
from src.l1_agent.db.models import jobs, knowledge_sources
from src.l1_agent.demo import DEMO_INCIDENTS, MockServiceNowClient, sample_sop_contents
from src.l1_agent.models.incident import Incident
from src.l1_agent.service.runs import record_by_number
from src.l1_agent.sop import ingest, store
from tests.conftest import make_service

ADMIN = Actor("user", "idp|admin")
ALICE = Actor("user", "idp|alice")
BOB = Actor("user", "idp|bob")
INTAKE = Actor.system("test-intake")
MQ = next(c for c in sample_sop_contents() if c["sop_id"] == "SOP-MQ-001")
CTX = StepContext(None, "run", "step")


class FakeSource:
    source_id = "fake_kb"

    def __init__(self, articles: List[Article], cursor: str = "c1") -> None:
        self.articles, self.cursor = articles, cursor
        self.asked: List[str] = []
        self.fail = False

    async def changed_since(self, cursor: str) -> Tuple[List[Article], str]:
        self.asked.append(cursor)
        if self.fail:
            raise RuntimeError("source down: password=hunter2")
        return self.articles, self.cursor

    async def fetch(self, article_id: str) -> Optional[Article]:
        return None

    async def close(self) -> None:
        return None


PROSE = Article(
    id="a" * 32,
    title="Queue depth runbook",
    body="<h1>Queue depth</h1><p>Check the depth &amp; the consumers.</p><script>alert(1)</script>",
    revision="3@2026-03-01 08:00:00",
    metadata={"number": "KB0010001", "keywords": "queue depth, mq", "category": "Middleware"},
)
STRUCTURED = Article(id="b" * 32, title="MQ", body=json.dumps(MQ), revision="1@2026-03-01")


class TestIngestion:
    async def test_a_prose_article_becomes_a_draft_with_no_steps_and_its_text(self, backend):
        tenant = await backend.new_tenant()
        result = await ingest.sync_source(backend.db, tenant, FakeSource([PROSE]))
        assert (result.articles, result.drafts, result.errors) == (1, 1, [])

        [version] = await store.list_versions(backend.db, tenant)
        review = await store.review(backend.db, tenant, uuid.UUID(version["id"]))
        assert version["status"] == "draft" and version["origin"] == "ingested"
        assert version["author"] == "system:ingest" and version["sop_id"] == "fake_kb-KB0010001"
        assert review["content"]["steps"] == [] and review["content"]["keywords"] == [
            "queue depth",
            "mq",
        ]
        assert review["source"] == {
            "id": "fake_kb",
            "article": "a" * 32,
            "revision": "3@2026-03-01 08:00:00",
        }
        # Stored as text. Markup is removed, so nothing from an article can run in the reviewer.
        assert review["source_text"] == "Queue depth\nCheck the depth & the consumers."
        assert review["problems"], "a draft with no steps is not valid and cannot be submitted"

    async def test_a_structured_article_keeps_its_steps_but_is_still_only_a_draft(self, backend):
        tenant = await backend.new_tenant()
        await ingest.sync_source(backend.db, tenant, FakeSource([STRUCTURED]))
        [version] = await store.list_versions(backend.db, tenant)
        review = await store.review(backend.db, tenant, uuid.UUID(version["id"]))
        assert version["sop_id"] == "SOP-MQ-001" and version["status"] == "draft"
        assert review["problems"] == [] and len(review["content"]["steps"]) == len(MQ["steps"])
        assert await store.published(backend.db, tenant) == [], "nothing ingested can run"

        # Even though it is valid and matches, the engine does not use it.
        service = make_service(backend.db, sops_from_store=True)
        incident = Incident.from_servicenow(DEMO_INCIDENTS[1])
        await service.ingest(tenant, incident, INTAKE)
        await service.work_once([tenant])
        record = await record_by_number(backend.db, tenant, incident.number)
        assert record["reason_code"] == "no_sop"

    async def test_an_ingested_article_needs_two_people_before_it_runs(self, backend):
        tenant = await backend.new_tenant()
        await ingest.sync_source(backend.db, tenant, FakeSource([STRUCTURED]))
        [version] = await store.list_versions(backend.db, tenant)
        digest = await store.submit(backend.db, tenant, ALICE, uuid.UUID(version["id"]))
        with pytest.raises(store.NotAllowed):
            await store.approve(backend.db, tenant, ALICE, uuid.UUID(version["id"]), digest)
        await store.approve(backend.db, tenant, BOB, uuid.UUID(version["id"]), digest)
        await store.publish(backend.db, tenant, BOB, uuid.UUID(version["id"]))
        events = await events_for(backend.db, tenant, correlation_id="sop:SOP-MQ-001")
        published = next(e for e in events if e["event_type"] == "sop.published")
        assert published["payload"]["origin"] == "ingested"

    async def test_the_same_revision_is_ingested_once_and_a_new_revision_again(self, backend):
        tenant = await backend.new_tenant()
        source = FakeSource([PROSE], cursor="c1")
        await ingest.sync_source(backend.db, tenant, source)
        again = await ingest.sync_source(backend.db, tenant, source)
        assert (again.drafts, again.skipped) == (0, 1)
        assert source.asked == ["", "c1"], "the second read starts from the stored cursor"

        edited = Article(**{**PROSE.__dict__, "revision": "4@2026-03-02 08:00:00"})
        source.articles, source.cursor = [edited], "c2"
        third = await ingest.sync_source(backend.db, tenant, source)
        assert third.drafts == 1
        assert len(await store.list_versions(backend.db, tenant)) == 2

    async def test_a_source_that_fails_is_recorded_and_the_cursor_stays(self, backend):
        tenant = await backend.new_tenant()
        source = FakeSource([PROSE], cursor="c1")
        await ingest.sync_source(backend.db, tenant, source)
        source.fail = True
        result = await ingest.sync_source(backend.db, tenant, source)
        assert result.errors == ["could not read the source: RuntimeError"]
        async with backend.db.tenant(tenant) as conn:
            row = (await conn.execute(sa.select(knowledge_sources))).one()
        assert row.cursor == "c1" and "RuntimeError" in row.last_error
        assert "hunter2" not in row.last_error
        synced = [
            e for e in await events_for(backend.db, tenant) if e["event_type"] == "knowledge.synced"
        ]
        assert len(synced) == 2 and "hunter2" not in json.dumps(synced)

    async def test_one_bad_article_does_not_stop_the_others(self, backend):
        tenant = await backend.new_tenant()
        bad = Article(
            id="c" * 32, title="x", body=json.dumps({"sop_id": "../x", "steps": []}), revision="1"
        )
        result = await ingest.sync_source(backend.db, tenant, FakeSource([bad, PROSE]))
        assert result.drafts == 1 and len(result.errors) == 1

    def test_plain_text_drops_markup(self):
        assert ingest.plain_text("<p>a<br/>b</p><img src=x onerror=alert(1)>") == "a\nb"


def settings(**parts) -> Settings:
    return Settings(**parts)


class TestConnections:
    async def test_a_connection_is_validated_against_the_manifest(self, backend):
        tenant = await backend.new_tenant()
        save = connections.set_connection
        with pytest.raises(ConfigError, match="Unknown connector"):
            await save(backend.db, tenant, ADMIN, "teleport", {}, {})
        with pytest.raises(ConfigError, match="base_url"):
            await save(backend.db, tenant, ADMIN, "splunk", {}, {})
        with pytest.raises(ConfigError, match="not allowed"):
            await save(backend.db, tenant, ADMIN, "splunk", {"base_url": "https://s", "x": 1}, {})
        with pytest.raises(ConfigError, match="no credential named"):
            await save(
                backend.db, tenant, ADMIN, "splunk", {"base_url": "https://s"}, {"pw": "env:X"}
            )
        assert await connections.stored_connections(backend.db, tenant) == {}

    async def test_a_credential_value_is_refused_only_a_reference_is_stored(self, backend):
        tenant = await backend.new_tenant()
        with pytest.raises(ConfigError, match="credential 'token'"):
            await connections.set_connection(
                backend.db,
                tenant,
                ADMIN,
                "splunk",
                {"base_url": "https://s"},
                {"token": "eyJhbGciOi-the-real-token"},
            )
        await connections.set_connection(
            backend.db,
            tenant,
            ADMIN,
            "splunk",
            {"base_url": "https://s"},
            {"token": "vault:splunk/token"},
        )
        stored = await connections.stored_connections(backend.db, tenant)
        assert stored["splunk"].secret_refs == {"token": "vault:splunk/token"}
        event = [
            e
            for e in await events_for(backend.db, tenant)
            if e["event_type"] == "connection.updated"
        ][0]
        assert event["payload"]["credentials"] == {"token": "vault:splunk/token"}
        assert event["actor"]["id"] == "idp|admin"

    async def test_each_tenant_gets_connectors_built_from_its_own_connections(self, backend):
        a, b = await backend.new_tenant("a"), await backend.new_tenant("b")
        hub = ConnectorHub(backend.db, settings(), SecretResolver({}), refresh_seconds=0)
        await connections.set_connection(
            backend.db, a, ADMIN, "splunk", {"base_url": "https://splunk-a.example"}, {}
        )
        await connections.set_connection(
            backend.db, b, ADMIN, "dynatrace", {"base_url": "https://dt-b.example"}, {}
        )
        for_a, for_b = await hub.for_tenant(a), await hub.for_tenant(b)
        assert for_a.connector("splunk").config["base_url"] == "https://splunk-a.example"
        assert for_a.connector("dynatrace") is None
        assert for_b.connector("splunk") is None and for_b.connector("dynatrace") is not None
        await hub.close()

    async def test_environment_settings_are_the_fallback_and_a_stored_connection_wins(
        self, backend, monkeypatch
    ):
        monkeypatch.setenv("SPLUNK_TOKEN", "from-env")
        tenant = await backend.new_tenant()
        deployment = settings(
            splunk=SplunkSettings(base_url="https://splunk-env.example"),
            dynatrace=DynatraceSettings(base_url="https://dt-env.example"),
        )
        hub = ConnectorHub(backend.db, deployment, SecretResolver({}), refresh_seconds=0)
        before = await hub.for_tenant(tenant)
        assert before.connector("splunk").config["base_url"] == "https://splunk-env.example"
        assert before.connector("splunk").secrets.slots == ("token",)

        await connections.set_connection(
            backend.db, tenant, ADMIN, "splunk", {"base_url": "https://splunk-own.example"}, {}
        )
        after = await hub.for_tenant(tenant)
        assert after.connector("splunk").config["base_url"] == "https://splunk-own.example"
        assert after.connector("dynatrace").config["base_url"] == "https://dt-env.example"

        described = {d["id"]: d for d in await hub.describe(tenant)}
        assert described["splunk"]["origin"] == "connection"
        assert described["dynatrace"]["origin"] == "environment"
        assert described["mainframe"]["configured"] is False
        assert "from-env" not in json.dumps(described)
        await hub.close()

    def test_environment_references_follow_the_secrets_backend(self, monkeypatch):
        monkeypatch.delenv("SPLUNK_TOKEN", raising=False)
        base = dict(splunk=SplunkSettings(base_url="https://s.example"))
        plain = environment_connections(settings(**base))["splunk"]
        vault = environment_connections(settings(**base, secrets=SecretsSettings(backend="vault")))[
            "splunk"
        ]
        explicit = environment_connections(
            settings(**base, secret_refs={"SPLUNK_TOKEN": "file:/run/secrets/splunk"})
        )["splunk"]
        assert plain.secret_refs == {}
        assert vault.secret_refs == {"token": "vault:splunk/token"}
        assert explicit.secret_refs == {"token": "file:/run/secrets/splunk"}

    async def test_a_disabled_or_broken_connection_does_not_take_the_others_down(self, backend):
        tenant = await backend.new_tenant()
        deployment = settings(
            windows_share=WindowsShareSettings(allowed_unc_prefixes=["//fs/logs"]),
        )
        hub = ConnectorHub(backend.db, deployment, SecretResolver({}), refresh_seconds=0)
        await connections.set_connection(
            backend.db,
            tenant,
            ADMIN,
            "splunk",
            {"base_url": "https://s.example"},
            {},
            enabled=False,
        )
        # Accepted when saved by an older build; no longer valid for this manifest.
        async with backend.db.tenant(tenant) as conn:
            await conn.execute(
                sa.insert(connections.connections).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant,
                    connector_id="dynatrace",
                    config={"endpoint": "https://old-key.example"},
                    secret_refs={},
                    enabled=True,
                    updated_by="test",
                    updated_at=connections.utcnow(),
                )
            )
        built = await hub.for_tenant(tenant)
        assert built.connector("splunk") is None and built.connector("dynatrace") is None
        assert built.connector("fileshare") is not None and built.connector("autosys") is not None
        described = {d["id"]: d for d in await hub.describe(tenant)}
        assert "base_url" in described["dynatrace"]["error"]
        assert [c.ok for c in await hub.test(tenant, "dynatrace")] == [False]
        with pytest.raises(LookupError):
            await hub.test(tenant, "teleport")
        await hub.close()

    async def test_a_step_for_a_connector_the_tenant_has_not_configured_needs_a_human(
        self, backend
    ):
        tenant = await backend.new_tenant()
        version = await store.create_draft(backend.db, tenant, ALICE, MQ)
        digest = await store.submit(backend.db, tenant, ALICE, version)
        await store.approve(backend.db, tenant, BOB, version, digest)
        await store.publish(backend.db, tenant, BOB, version)

        from src.l1_agent.engine.executor import SOPExecutor
        from src.l1_agent.engine.incident_processor import IncidentProcessor
        from src.l1_agent.engine.sop_matcher import SOPMatcher
        from src.l1_agent.service.runs import RunService

        hub = ConnectorHub(backend.db, settings(), SecretResolver({}), refresh_seconds=0)
        processor = IncidentProcessor(
            snow_client=MockServiceNowClient(),
            sop_matcher=SOPMatcher(0.6),
            sop_parser=None,
            executor=SOPExecutor(connectors=hub, retry_max_attempts=1, retry_base_delay=0),
        )
        service = RunService(backend.db, processor, sops_from_store=True)
        incident = Incident.from_servicenow(DEMO_INCIDENTS[1])
        await service.ingest(tenant, incident, INTAKE)
        await service.work_once([tenant])
        record = await record_by_number(backend.db, tenant, incident.number)
        assert record["outcome"] == "needs_human"
        assert "Adapter 'ir360' not configured" in record["escalation_reason"]
        await hub.close()

    async def test_removing_a_connection_is_audited(self, backend):
        tenant = await backend.new_tenant()
        await connections.set_connection(
            backend.db, tenant, ADMIN, "autosys", {"cli_path": "autorep"}, {}
        )
        assert await connections.remove_connection(backend.db, tenant, ADMIN, "autosys")
        assert not await connections.remove_connection(backend.db, tenant, ADMIN, "autosys")
        types = [e["event_type"] for e in await events_for(backend.db, tenant)]
        assert types == ["connection.updated", "connection.removed"]


class ClaimingSnow(MockServiceNowClient):
    def __init__(self, result=None, error: Optional[Exception] = None) -> None:
        super().__init__()
        self.result, self.error, self.claims = result or ClaimResult(True), error, 0

    async def claim(self, incident):
        self.claims += 1
        if self.error:
            raise self.error
        return self.result


class TestClaim:
    async def run(self, backend, snow, **kwargs):
        tenant = await backend.new_tenant()
        service = make_service(backend.db, snow=snow, **kwargs)
        incident = Incident.from_servicenow(DEMO_INCIDENTS[1])
        await service.ingest(tenant, incident, INTAKE)
        return tenant, service, incident

    async def test_a_claimed_ticket_is_worked(self, backend):
        snow = ClaimingSnow()
        tenant, service, incident = await self.run(backend, snow)
        await service.work_once([tenant])
        record = await record_by_number(backend.db, tenant, incident.number)
        assert record["outcome"] == "resolved" and snow.claims == 1

    async def test_a_ticket_someone_else_holds_is_not_touched(self, backend):
        snow = ClaimingSnow(ClaimResult(False, "the ticket is assigned to someone else"))
        tenant, service, incident = await self.run(backend, snow)
        await service.work_once([tenant])
        record = await record_by_number(backend.db, tenant, incident.number)
        assert record["outcome"] == "aborted"
        assert record["escalation_reason"] == "Not claimed: the ticket is assigned to someone else"
        assert record["step_count"] == 0
        assert snow.work_notes == [] and snow.updates == [], "no note, no reassignment"
        concluded = [
            e for e in await events_for(backend.db, tenant) if e["event_type"] == "run.concluded"
        ]
        assert concluded[0]["payload"]["outcome"] == "aborted"

    async def test_when_the_itsm_cannot_be_reached_the_run_is_tried_again_not_guessed(
        self, backend
    ):
        snow = ClaimingSnow(error=ConnectionError("servicenow unreachable"))
        tenant, service, incident = await self.run(backend, snow, max_attempts=2)
        await service.work_once([tenant])
        assert await record_by_number(backend.db, tenant, incident.number) is None
        async with backend.db.tenant(tenant) as conn:
            job = (await conn.execute(sa.select(jobs))).one()
        assert job.status == "queued" and "could not claim" in job.last_error
        assert snow.work_notes == []

        # The ITSM comes back: the same run completes.
        snow.error = None
        async with backend.db.tenant(tenant) as conn:
            await conn.execute(sa.update(jobs).values(available_at=connections.utcnow()))
        await service.work_once([tenant])
        record = await record_by_number(backend.db, tenant, incident.number)
        assert record["outcome"] == "resolved"

    async def test_a_run_that_can_never_claim_ends_with_a_person(self, backend):
        snow = ClaimingSnow(error=ConnectionError("down"))
        tenant, service, incident = await self.run(backend, snow, max_attempts=1)
        for _ in range(2):
            async with backend.db.tenant(tenant) as conn:
                await conn.execute(sa.update(jobs).values(available_at=connections.utcnow()))
            await service.work_once([tenant])
        record = await record_by_number(backend.db, tenant, incident.number)
        assert record["outcome"] == "needs_human"
