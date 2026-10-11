"""SOP versions: who may do what, what the engine can see, and what the database refuses.

Phase 3 exit criteria 3 and 4:
  3. a draft cannot be selected by the matcher, checked at the data layer;
  4. the same user cannot author and approve one version, by constraint and in the service.
"""

from __future__ import annotations

import copy
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.l1_agent.audit.log import Actor, events_for, verify
from src.l1_agent.db.engine import utcnow
from src.l1_agent.db.models import published_sops, runs, sop_versions, sops
from src.l1_agent.demo import DEMO_INCIDENTS, MockServiceNowClient, sample_sop_contents
from src.l1_agent.models.incident import Incident
from src.l1_agent.service.runs import record_by_number
from src.l1_agent.sop import store
from tests.conftest import make_service

ALICE = Actor("user", "idp|alice")
BOB = Actor("user", "idp|bob")
CAROL = Actor("user", "idp|carol")
INTAKE = Actor.system("test-intake")
MQ = next(c for c in sample_sop_contents() if c["sop_id"] == "SOP-MQ-001")
MQ_INCIDENT = DEMO_INCIDENTS[1]  # ORDER.REQUEST: the verified-resolution scenario


def content(**changes) -> dict:
    data = copy.deepcopy(MQ)
    data.update(changes)
    return data


async def in_review(db, tenant, body=None, author=ALICE):
    version_id = await store.create_draft(db, tenant, author, body or content())
    digest = await store.submit(db, tenant, author, version_id)
    return version_id, digest


async def publish(db, tenant, body=None, author=ALICE, approver=BOB):
    version_id, digest = await in_review(db, tenant, body, author)
    await store.approve(db, tenant, approver, version_id, digest)
    number = await store.publish(db, tenant, approver, version_id)
    return version_id, number


async def status_of(db, tenant, version_id) -> str:
    async with db.tenant(tenant) as conn:
        return (
            await conn.execute(
                sa.select(sop_versions.c.status).where(sop_versions.c.id == version_id)
            )
        ).scalar_one()


async def insert_version(conn, tenant, **values) -> uuid.UUID:
    """A row written straight to the table, the way a faulty code path might."""
    sop_id = (
        await conn.execute(sa.select(sops.c.id).where(sops.c.tenant_id == tenant).limit(1))
    ).scalar()
    if sop_id is None:
        sop_id = uuid.uuid4()
        await conn.execute(
            sa.insert(sops).values(
                id=sop_id, tenant_id=tenant, sop_key="SOP-RAW", created_at=utcnow()
            )
        )
    version_id = uuid.uuid4()
    row = dict(
        id=version_id,
        tenant_id=tenant,
        sop_id=sop_id,
        status="draft",
        content=content(sop_id="SOP-RAW"),
        origin="authored",
        author_id="user:idp|alice",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    row.update(values)
    await conn.execute(sa.insert(sop_versions).values(**row))
    return version_id


class TestOnlyPublishedVersionsReachTheEngine:
    """Exit criterion 3."""

    async def test_no_unpublished_state_is_visible_through_the_engines_view(self, backend):
        tenant = await backend.new_tenant()
        db = backend.db
        await store.create_draft(db, tenant, ALICE, content(sop_id="SOP-DRAFT"))
        await in_review(db, tenant, content(sop_id="SOP-REVIEW"))
        approved, digest = await in_review(db, tenant, content(sop_id="SOP-APPROVED"))
        await store.approve(db, tenant, BOB, approved, digest)
        await publish(db, tenant, content(sop_id="SOP-RETIRED"))
        await store.retire(db, tenant, BOB, "SOP-RETIRED")
        await publish(db, tenant, content(sop_id="SOP-OLD", title="first"))
        await publish(db, tenant, content(sop_id="SOP-OLD", title="second"))  # supersedes

        visible = await store.published(db, tenant)
        assert [(s.sop_id, s.title, s.version) for s in visible] == [("SOP-OLD", "second", "2")]
        async with db.tenant(tenant) as conn:
            rows = (await conn.execute(sa.select(published_sops))).all()
            statuses = {
                r.status
                for r in await conn.execute(
                    sa.select(sop_versions.c.status).where(sop_versions.c.tenant_id == tenant)
                )
            }
        assert len(rows) == 1 and rows[0].sop_key == "SOP-OLD"
        assert statuses == {"draft", "in_review", "approved", "retired", "superseded", "published"}

    async def test_a_draft_of_a_perfectly_matching_sop_is_not_run(self, backend):
        tenant = await backend.new_tenant()
        await in_review(backend.db, tenant)  # valid, submitted, not approved
        service = make_service(backend.db, sops_from_store=True)
        incident = Incident.from_servicenow(MQ_INCIDENT)
        await service.ingest(tenant, incident, INTAKE)
        await service.work_once([tenant])
        record = await record_by_number(backend.db, tenant, incident.number)
        assert record["outcome"] == "escalated" and record["reason_code"] == "no_sop"
        assert record["step_count"] == 0

    async def test_the_same_sop_runs_once_it_is_published_and_the_run_records_its_hash(
        self, backend
    ):
        tenant = await backend.new_tenant()
        version_id, number = await publish(backend.db, tenant)
        service = make_service(backend.db, sops_from_store=True)
        incident = Incident.from_servicenow(MQ_INCIDENT)
        await service.ingest(tenant, incident, INTAKE)
        await service.work_once([tenant])

        record = await record_by_number(backend.db, tenant, incident.number)
        assert record["outcome"] == "resolved" and record["sop_id"] == "SOP-MQ-001"
        digest = store.content_hash(store.normalise(content()))
        assert record["sop_version"] == str(number) == "1" and record["sop_hash"] == digest
        matched = [
            e
            for e in await events_for(backend.db, tenant, correlation_id=record["run_id"])
            if e["event_type"] == "run.sop_matched"
        ]
        assert matched[0]["payload"]["sop_sha256"] == digest

    async def test_a_row_marked_published_without_an_approver_cannot_exist(self, backend):
        """Even a direct write cannot make an unapproved version visible to the engine."""
        tenant = await backend.new_tenant()
        with pytest.raises(IntegrityError):
            async with backend.db.tenant(tenant) as conn:
                await insert_version(conn, tenant, status="published", version=1)
        with pytest.raises(IntegrityError):
            async with backend.db.tenant(tenant) as conn:
                await insert_version(
                    conn, tenant, status="published", version=1, approver_id="user:idp|bob"
                )  # an approver but no content hash
        assert await store.published(backend.db, tenant) == []

    async def test_an_unknown_status_cannot_be_stored(self, backend):
        tenant = await backend.new_tenant()
        with pytest.raises(IntegrityError):
            async with backend.db.tenant(tenant) as conn:
                await insert_version(conn, tenant, status="live")

    async def test_two_published_versions_of_one_sop_cannot_coexist(self, backend):
        tenant = await backend.new_tenant()
        await publish(backend.db, tenant)
        with pytest.raises(IntegrityError):
            async with backend.db.tenant(tenant) as conn:
                await insert_version(
                    conn,
                    tenant,
                    status="published",
                    version=2,
                    approver_id="user:idp|bob",
                    content_hash="a" * 64,
                )


class TestAuthorCannotApprove:
    """Exit criterion 4."""

    async def test_the_service_refuses_the_author(self, backend):
        tenant = await backend.new_tenant()
        version_id, digest = await in_review(backend.db, tenant)
        with pytest.raises(store.NotAllowed, match="author"):
            await store.approve(backend.db, tenant, ALICE, version_id, digest)
        assert await status_of(backend.db, tenant, version_id) == "in_review"
        await store.approve(backend.db, tenant, BOB, version_id, digest)
        assert await status_of(backend.db, tenant, version_id) == "approved"

    async def test_the_database_refuses_it_whatever_the_code_does(self, backend):
        tenant = await backend.new_tenant()
        version_id, digest = await in_review(backend.db, tenant)
        with pytest.raises(IntegrityError):
            async with backend.db.tenant(tenant) as conn:
                await conn.execute(
                    sa.update(sop_versions)
                    .where(sop_versions.c.id == version_id)
                    .values(status="approved", approver_id="user:idp|alice")
                )
        with pytest.raises(IntegrityError):
            async with backend.db.tenant(tenant) as conn:
                await insert_version(
                    conn,
                    tenant,
                    status="approved",
                    content_hash="a" * 64,
                    author_id="user:idp|alice",
                    approver_id="user:idp|alice",
                )
        assert await status_of(backend.db, tenant, version_id) == "in_review"

    async def test_adopting_an_ingested_draft_makes_the_editor_its_author(self, backend):
        tenant = await backend.new_tenant()
        ingest = Actor.system("ingest")
        version_id = await store.create_draft(
            backend.db, tenant, ingest, content(), origin=store.ORIGIN_INGESTED
        )
        digest = await store.submit(backend.db, tenant, ALICE, version_id)
        with pytest.raises(store.NotAllowed):
            await store.approve(backend.db, tenant, ALICE, version_id, digest)
        await store.approve(backend.db, tenant, BOB, version_id, digest)

    async def test_only_the_author_can_edit_or_submit_a_persons_draft(self, backend):
        tenant = await backend.new_tenant()
        version_id = await store.create_draft(backend.db, tenant, ALICE, content())
        with pytest.raises(store.NotAllowed):
            await store.update_draft(backend.db, tenant, BOB, version_id, content(title="mine"))
        with pytest.raises(store.NotAllowed):
            await store.submit(backend.db, tenant, BOB, version_id)

    async def test_approval_is_of_the_content_that_was_reviewed(self, backend):
        tenant = await backend.new_tenant()
        version_id, digest = await in_review(backend.db, tenant)
        with pytest.raises(store.WrongState, match="changed since"):
            await store.approve(backend.db, tenant, BOB, version_id, "0" * 64)
        with pytest.raises(store.WrongState):
            await store.approve(backend.db, tenant, BOB, version_id, "")
        await store.approve(backend.db, tenant, BOB, version_id, digest)


class TestLifecycle:
    async def test_each_step_is_allowed_only_from_the_state_before_it(self, backend):
        tenant = await backend.new_tenant()
        db = backend.db
        version_id = await store.create_draft(db, tenant, ALICE, content())
        with pytest.raises(store.WrongState):
            await store.approve(db, tenant, BOB, version_id, "0" * 64)
        with pytest.raises(store.WrongState):
            await store.publish(db, tenant, BOB, version_id)
        digest = await store.submit(db, tenant, ALICE, version_id)
        with pytest.raises(store.WrongState):
            await store.update_draft(db, tenant, ALICE, version_id, content(title="late edit"))
        with pytest.raises(store.WrongState):
            await store.discard(db, tenant, ALICE, version_id)
        with pytest.raises(store.WrongState):
            await store.publish(db, tenant, BOB, version_id)
        await store.approve(db, tenant, BOB, version_id, digest)
        with pytest.raises(store.WrongState):
            await store.request_changes(db, tenant, BOB, version_id, "too late")
        assert await store.publish(db, tenant, BOB, version_id) == 1
        with pytest.raises(store.WrongState):
            await store.publish(db, tenant, BOB, version_id)

    async def test_changes_requested_returns_the_draft_to_its_author(self, backend):
        tenant = await backend.new_tenant()
        db = backend.db
        version_id, first = await in_review(db, tenant)
        await store.request_changes(db, tenant, BOB, version_id, "Name the queue in the title")
        review = await store.review(db, tenant, version_id)
        assert review["status"] == "draft" and review["review_note"].startswith("Name the queue")
        await store.update_draft(db, tenant, ALICE, version_id, content(title="MQ depth, by queue"))
        second = await store.submit(db, tenant, ALICE, version_id)
        assert second != first
        with pytest.raises(store.WrongState):
            await store.approve(db, tenant, BOB, version_id, first)  # the old review is void
        await store.approve(db, tenant, BOB, version_id, second)

    async def test_an_invalid_sop_cannot_be_submitted(self, backend):
        tenant = await backend.new_tenant()
        unverified = content()
        unverified["steps"] = [
            {"step_id": "s1", "step_type": "NOTE", "parameters": {"text": "x"}, "on_success": "s2"},
            {"step_id": "s2", "step_type": "RESOLVE", "parameters": {"text": "done"}},
        ]
        version_id = await store.create_draft(backend.db, tenant, ALICE, unverified)
        with pytest.raises(store.SopError) as err:
            await store.submit(backend.db, tenant, ALICE, version_id)
        assert any("without a passed VERIFY" in p for p in err.value.problems)
        assert await status_of(backend.db, tenant, version_id) == "draft"

    async def test_an_sop_naming_an_unknown_capability_cannot_be_submitted(self, backend):
        tenant = await backend.new_tenant()
        body = content()
        body["steps"][1] = {
            **body["steps"][1],
            "step_type": "CHECK",
            "capability": "ibm_mq.queue.purge",
        }
        version_id = await store.create_draft(backend.db, tenant, ALICE, body)
        with pytest.raises(store.SopError) as err:
            await store.submit(backend.db, tenant, ALICE, version_id)
        assert any("unknown capability 'ibm_mq.queue.purge'" in p for p in err.value.problems)

    async def test_publishing_supersedes_and_numbers_versions(self, backend):
        tenant = await backend.new_tenant()
        first, n1 = await publish(backend.db, tenant, content(title="one"))
        second, n2 = await publish(
            backend.db, tenant, content(title="two"), author=BOB, approver=CAROL
        )
        assert (n1, n2) == (1, 2)
        assert await status_of(backend.db, tenant, first) == "superseded"
        assert [s.title for s in await store.published(backend.db, tenant)] == ["two"]
        review = await store.review(backend.db, tenant, second)
        assert review["version"] == 2 and review["approver"] == "user:idp|carol"

    async def test_retiring_removes_the_sop_from_matching(self, backend):
        tenant = await backend.new_tenant()
        await publish(backend.db, tenant)
        await store.retire(backend.db, tenant, BOB, "SOP-MQ-001")
        assert await store.published(backend.db, tenant) == []
        with pytest.raises(store.NotFound):
            await store.retire(backend.db, tenant, BOB, "SOP-MQ-001")

    async def test_the_hash_ignores_lifecycle_fields_and_unknown_keys(self, backend):
        base = store.content_hash(store.normalise(content()))
        noisy = content(status="published", version="99.0", injected="ignored")
        assert store.content_hash(store.normalise(noisy)) == base
        assert store.content_hash(store.normalise(content(title="other"))) != base

    @pytest.mark.parametrize("bad", [[], "x", {"title": "no id"}, {"sop_id": "../etc"}])
    async def test_content_that_is_not_an_sop_is_refused(self, backend, bad):
        tenant = await backend.new_tenant()
        with pytest.raises(store.SopError):
            await store.create_draft(backend.db, tenant, ALICE, bad)

    async def test_the_reviewer_sees_checks_capabilities_and_what_changed(self, backend):
        tenant = await backend.new_tenant()
        await publish(backend.db, tenant)
        changed = content(title="MQ depth v2")
        changed["steps"][3]["parameters"]["query"] = "index=app_logs ERROR"
        changed["steps"] = [s for s in changed["steps"] if s["step_id"] != "step-5"]
        changed["steps"][3]["on_success"] = "step-6"
        version_id, digest = await in_review(backend.db, tenant, changed, author=CAROL)

        review = await store.review(backend.db, tenant, version_id)
        assert review["problems"] == [] and review["current_hash"] == digest
        assert review["diff"]["fields"] == ["title"]
        assert {(s["step_id"], s["change"]) for s in review["diff"]["steps"]} == {
            ("step-4", "changed"),
            ("step-5", "removed"),
        }
        used = {c["capability"]: c for c in review["capabilities"]}
        assert set(used) == {"ibm_mq.queue.check", "splunk.search"}
        assert all(c["kind"] == "read" and c["tier"] == "T0" for c in used.values())

    async def test_every_transition_is_in_the_audit_log(self, backend):
        tenant = await backend.new_tenant()
        version_id, digest = await in_review(backend.db, tenant)
        await store.request_changes(backend.db, tenant, BOB, version_id, "again")
        digest = await store.submit(backend.db, tenant, ALICE, version_id)
        await store.approve(backend.db, tenant, BOB, version_id, digest)
        await store.publish(backend.db, tenant, CAROL, version_id)
        await store.retire(backend.db, tenant, CAROL, "SOP-MQ-001")

        events = await events_for(backend.db, tenant, correlation_id="sop:SOP-MQ-001")
        assert [(e["event_type"], e["actor"]["id"]) for e in events] == [
            ("sop.draft_created", "idp|alice"),
            ("sop.submitted", "idp|alice"),
            ("sop.changes_requested", "idp|bob"),
            ("sop.submitted", "idp|alice"),
            ("sop.approved", "idp|bob"),
            ("sop.published", "idp|carol"),
            ("sop.retired", "idp|carol"),
        ]
        published = next(e for e in events if e["event_type"] == "sop.published")["payload"]
        assert published["content_sha256"] == digest
        assert (published["author"], published["approver"]) == ("user:idp|alice", "user:idp|bob")
        assert (await verify(backend.db, tenant)).ok


class TestImmutability:
    """PostgreSQL only: a trigger guards the table against any writer, the owner included."""

    @pytest.mark.parametrize(
        "values",
        [
            {"content": {"sop_id": "SOP-MQ-001", "steps": []}},
            {"content_hash": "f" * 64},
            {"author_id": "user:idp|mallory"},
        ],
        ids=["content", "hash", "author"],
    )
    async def test_a_submitted_version_cannot_be_changed(self, pg, values):
        tenant = await pg.new_tenant()
        version_id, _ = await in_review(pg.db, tenant)
        with pytest.raises(DBAPIError, match="cannot be changed"):
            async with pg.db.tenant(tenant) as conn:
                await conn.execute(
                    sa.update(sop_versions).where(sop_versions.c.id == version_id).values(**values)
                )

    @pytest.mark.parametrize(
        "values",
        [
            {"approver_id": "user:idp|mallory"},
            {"status": "draft"},
            {"version": 7},
            {"content": {"sop_id": "SOP-MQ-001", "steps": []}},
        ],
        ids=["approver", "back-to-draft", "renumber", "content"],
    )
    async def test_a_published_version_cannot_be_changed(self, pg, values):
        tenant = await pg.new_tenant()
        version_id, _ = await publish(pg.db, tenant)
        with pytest.raises(DBAPIError, match="cannot be changed"):
            async with pg.db.tenant(tenant) as conn:
                await conn.execute(
                    sa.update(sop_versions).where(sop_versions.c.id == version_id).values(**values)
                )

    async def test_only_a_draft_can_be_deleted(self, pg):
        tenant = await pg.new_tenant()
        version_id, _ = await publish(pg.db, tenant)
        with pytest.raises(DBAPIError, match="only a draft"):
            async with pg.db.tenant(tenant) as conn:
                await conn.execute(sa.delete(sop_versions).where(sop_versions.c.id == version_id))
        draft = await store.create_draft(pg.db, tenant, ALICE, content(sop_id="SOP-TMP"))
        await store.discard(pg.db, tenant, ALICE, draft)

    async def test_the_owner_role_is_held_to_the_same_rule(self, pg):
        tenant = await pg.new_tenant()
        version_id, _ = await publish(pg.db, tenant)
        with pytest.raises(DBAPIError, match="cannot be changed"):
            async with pg._owner.tenant(tenant) as conn:
                await conn.execute(
                    sa.update(sop_versions)
                    .where(sop_versions.c.id == version_id)
                    .values(content={"sop_id": "SOP-MQ-001", "steps": []})
                )

    async def test_the_view_shows_only_the_callers_tenant(self, pg):
        a, b = await pg.new_tenant("a"), await pg.new_tenant("b")
        await publish(pg.db, a)
        await publish(pg.db, b, content(sop_id="SOP-B-ONLY"))
        async with pg.db.tenant(a) as conn:
            keys = {r.sop_key for r in await conn.execute(sa.select(published_sops))}
        async with pg.db.platform() as conn:
            none = (await conn.execute(sa.select(published_sops))).all()
        assert keys == {"SOP-MQ-001"} and none == []


class TestResume:
    async def test_a_run_finishes_on_the_version_it_started_with(self, backend):
        """A newer version published mid-run does not change what an interrupted run executes."""
        tenant = await backend.new_tenant()
        db = backend.db
        _, number = await publish(db, tenant)
        first_hash = store.content_hash(store.normalise(content()))
        incident = Incident.from_servicenow(MQ_INCIDENT)
        service = make_service(db, sops_from_store=True)
        await service.ingest(tenant, incident, INTAKE)
        job = await service.claim(tenant)
        async with db.tenant(tenant) as conn:  # as if a worker matched, then died
            await conn.execute(
                sa.update(runs)
                .where(runs.c.id == job.run_id)
                .values(
                    sop_id="SOP-MQ-001",
                    sop_version=str(number),
                    sop_hash=first_hash,
                    confidence=0.9,
                    match_rationale="earlier attempt",
                )
            )
        await publish(db, tenant, content(title="rewritten"), author=BOB, approver=CAROL)

        await service.execute(tenant, job)
        record = await record_by_number(db, tenant, incident.number)
        assert record["outcome"] == "resolved"
        assert record["sop_version"] == "1" and record["sop_hash"] == first_hash
        assert record["sop_title"] == MQ["title"], "the version that started, not the new one"

    async def test_a_retired_version_does_not_finish_an_interrupted_run(self, backend):
        tenant = await backend.new_tenant()
        db = backend.db
        _, number = await publish(db, tenant)
        digest = store.content_hash(store.normalise(content()))
        incident = Incident.from_servicenow(MQ_INCIDENT)
        snow = MockServiceNowClient()
        service = make_service(db, snow=snow, sops_from_store=True)
        await service.ingest(tenant, incident, INTAKE)
        job = await service.claim(tenant)
        async with db.tenant(tenant) as conn:
            await conn.execute(
                sa.update(runs)
                .where(runs.c.id == job.run_id)
                .values(
                    sop_id="SOP-MQ-001",
                    sop_version=str(number),
                    sop_hash=digest,
                    confidence=0.9,
                    match_rationale="earlier attempt",
                )
            )
        await store.retire(db, tenant, BOB, "SOP-MQ-001")

        await service.execute(tenant, job)
        record = await record_by_number(db, tenant, incident.number)
        assert record["outcome"] == "needs_human"
        assert "no longer available" in record["escalation_reason"]
