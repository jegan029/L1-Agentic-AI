"""The audit log is append only and `verify` notices tampering.

Phase 2 exit criterion 4: a modified row, a deleted row and a reordered row.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import sqlalchemy as sa

from src.l1_agent.audit.log import (
    GENESIS_HASH,
    Actor,
    append,
    checkpoint,
    event_hash,
    events_for,
    verify,
)
from src.l1_agent.db.models import audit_events

ALICE = Actor("user", "alice@idp")
KEY = "checkpoint-signing-key-for-tests"


async def write_chain(backend, n: int = 6) -> uuid.UUID:
    tenant = await backend.new_tenant()
    for i in range(n):
        async with backend.db.tenant(tenant) as conn:
            await append(conn, tenant, ALICE, "test.event", {"n": i}, correlation_id="run-1")
    return tenant


class TestAppend:
    async def test_events_form_a_gapless_chain(self, backend):
        tenant = await write_chain(backend, 5)

        async with backend.db.tenant(tenant) as conn:
            rows = (await conn.execute(sa.select(audit_events).order_by(audit_events.c.seq))).all()
        assert [r.seq for r in rows] == [1, 2, 3, 4, 5]
        assert rows[0].prev_hash == GENESIS_HASH
        assert all(rows[i].prev_hash == rows[i - 1].hash for i in range(1, 5))
        result = await verify(backend.db, tenant)
        assert result.ok and result.events_checked == 5 and result.head_seq == 5

    async def test_each_tenant_has_its_own_chain(self, backend):
        one, two = await write_chain(backend, 3), await write_chain(backend, 2)
        assert (await verify(backend.db, one)).head_seq == 3
        assert (await verify(backend.db, two)).head_seq == 2

    async def test_concurrent_appends_keep_the_chain_intact(self, backend):
        tenant = await backend.new_tenant()

        async def one(i: int) -> None:
            async with backend.db.tenant(tenant) as conn:
                await append(conn, tenant, ALICE, "test.concurrent", {"i": i})

        await asyncio.gather(*(one(i) for i in range(25)))

        result = await verify(backend.db, tenant)
        assert result.ok, result.problems
        assert result.events_checked == 25

    async def test_event_is_not_written_when_its_transaction_fails(self, backend):
        tenant = await write_chain(backend, 2)
        with pytest.raises(RuntimeError):
            async with backend.db.tenant(tenant) as conn:
                await append(conn, tenant, ALICE, "test.rolled_back", {})
                raise RuntimeError("the state change failed")
        assert (await verify(backend.db, tenant)).head_seq == 2

    async def test_secrets_in_free_text_are_redacted_before_they_are_chained(self, backend):
        tenant = await backend.new_tenant()
        async with backend.db.tenant(tenant) as conn:
            await append(
                conn, tenant, ALICE, "test.text", {"note": "login failed password=hunter2secret"}
            )
        stored = (await events_for(backend.db, tenant))[0]
        assert "hunter2secret" not in json.dumps(stored)

    async def test_timeline_for_one_run_comes_from_audit_events_alone(self, backend):
        tenant = await backend.new_tenant()
        async with backend.db.tenant(tenant) as conn:
            await append(conn, tenant, ALICE, "a", {}, correlation_id="run-1")
            await append(conn, tenant, ALICE, "b", {}, correlation_id="run-2")
            await append(conn, tenant, ALICE, "c", {}, correlation_id="run-1")
        timeline = await events_for(backend.db, tenant, correlation_id="run-1")
        assert [e["event_type"] for e in timeline] == ["a", "c"]


class TestVerifyDetectsTampering:
    async def test_modified_payload(self, backend):
        tenant = await write_chain(backend)
        await backend.tamper(
            "UPDATE audit_events SET payload = '{\"n\":999}' WHERE tenant_id = $1 AND seq = 3",
            _id(backend, tenant),
        )
        result = await verify(backend.db, tenant)
        assert not result.ok
        assert any("event 3" in p and "content" in p for p in result.problems)

    @pytest.mark.parametrize("column", ["actor_id", "event_type", "occurred_at", "correlation_id"])
    async def test_modified_metadata(self, backend, column):
        tenant = await write_chain(backend)
        await backend.tamper(
            f"UPDATE audit_events SET {column} = 'forged' WHERE tenant_id = $1 AND seq = 2",
            _id(backend, tenant),
        )
        assert not (await verify(backend.db, tenant)).ok

    async def test_deleted_row(self, backend):
        tenant = await write_chain(backend)
        await backend.tamper(
            "DELETE FROM audit_events WHERE tenant_id = $1 AND seq = 4", _id(backend, tenant)
        )
        result = await verify(backend.db, tenant)
        assert not result.ok
        assert any("gap" in p for p in result.problems)

    async def test_reordered_rows(self, backend):
        tenant = await write_chain(backend)
        tid = _id(backend, tenant)
        # Swap the positions of events 2 and 5.
        await backend.tamper(
            "UPDATE audit_events SET seq = 9002 WHERE tenant_id = $1 AND seq = 2", tid
        )
        await backend.tamper(
            "UPDATE audit_events SET seq = 2 WHERE tenant_id = $1 AND seq = 5", tid
        )
        await backend.tamper(
            "UPDATE audit_events SET seq = 5 WHERE tenant_id = $1 AND seq = 9002", tid
        )
        result = await verify(backend.db, tenant)
        assert not result.ok
        assert any("event 2" in p for p in result.problems)
        assert any("event 5" in p for p in result.problems)

    async def test_inserted_row(self, backend):
        tenant = await write_chain(backend, 3)
        tid = _id(backend, tenant)
        await backend.tamper(
            "INSERT INTO audit_events (tenant_id, seq, occurred_at, actor_type, actor_id, "
            "correlation_id, event_type, payload, prev_hash, hash) VALUES "
            f"($1, 4, '2026-01-01T00:00:00+00:00', 'user', 'mallory', '', 'forged', '{{}}', "
            f"'{'a' * 64}', '{'b' * 64}')",
            tid,
        )
        assert not (await verify(backend.db, tenant)).ok

    async def test_removed_tail_is_caught_by_a_checkpoint(self, backend):
        tenant = await write_chain(backend, 6)
        saved = await checkpoint(backend.db, tenant, KEY)
        await backend.tamper(
            "DELETE FROM audit_events WHERE tenant_id = $1 AND seq > 4", _id(backend, tenant)
        )

        without = await verify(backend.db, tenant)
        with_checkpoint = await verify(backend.db, tenant, checkpoints=[saved], checkpoint_key=KEY)

        assert without.ok, "a cut-off chain is still internally consistent"
        assert not with_checkpoint.ok
        assert any("no longer reaches" in p for p in with_checkpoint.problems)

    async def test_fully_rewritten_chain_is_caught_by_a_checkpoint(self, backend):
        tenant = await write_chain(backend, 3)
        saved = await checkpoint(backend.db, tenant, KEY)
        tid = _id(backend, tenant)
        # Rewrite event 3 and recompute its hash, as an attacker who knows the scheme would.
        async with backend.db.tenant(tenant) as conn:
            row = (await conn.execute(sa.select(audit_events).where(audit_events.c.seq == 3))).one()
        forged = event_hash(
            tenant,
            3,
            row.occurred_at,
            "user",
            "mallory",
            "",
            "test.event",
            row.payload,
            row.prev_hash,
        )
        await backend.tamper(
            "UPDATE audit_events SET actor_id = 'mallory', correlation_id = '', "
            f"hash = '{forged}' WHERE tenant_id = $1 AND seq = 3",
            tid,
        )

        assert (await verify(backend.db, tenant)).ok, "the forged chain is self-consistent"
        result = await verify(backend.db, tenant, checkpoints=[saved], checkpoint_key=KEY)
        assert any("rewritten" in p for p in result.problems)

    async def test_forged_checkpoint_is_rejected(self, backend):
        tenant = await write_chain(backend, 3)
        saved = await checkpoint(backend.db, tenant, KEY)
        forged = {**saved, "hash": "c" * 64}
        result = await verify(backend.db, tenant, checkpoints=[forged], checkpoint_key=KEY)
        assert any("signature" in p for p in result.problems)

    async def test_checkpoint_of_a_broken_chain_is_refused(self, backend):
        tenant = await write_chain(backend, 3)
        await backend.tamper(
            "DELETE FROM audit_events WHERE tenant_id = $1 AND seq = 2", _id(backend, tenant)
        )
        with pytest.raises(RuntimeError):
            await checkpoint(backend.db, tenant, KEY)


class TestAppendOnlyInPostgres:
    """The application role cannot change audit rows; nor can the owner while the guard is on."""

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE audit_events SET actor_id = 'x'",
            "DELETE FROM audit_events",
            "TRUNCATE audit_events",
        ],
    )
    async def test_application_role_is_refused(self, pg, statement):
        tenant = await write_chain(pg, 2)
        with pytest.raises(sa.exc.DBAPIError, match="permission denied"):
            async with pg.db.tenant(tenant) as conn:
                await conn.execute(sa.text(statement))
        assert (await verify(pg.db, tenant)).events_checked == 2

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE audit_events SET actor_id = 'x'",
            "DELETE FROM audit_events",
            "TRUNCATE audit_events",
        ],
    )
    async def test_owner_is_stopped_by_the_trigger(self, pg, statement):
        import asyncpg

        tenant = await write_chain(pg, 2)
        conn = await asyncpg.connect(pg.admin_dsn)
        try:
            with pytest.raises(asyncpg.PostgresError, match="append only"):
                await conn.execute(statement)
        finally:
            await conn.close()
        assert (await verify(pg.db, tenant)).ok


def _id(backend, tenant: uuid.UUID):
    """SQLite stores a UUID as 32 hex characters; asyncpg takes the UUID itself."""
    return tenant.hex if backend.name == "sqlite" else tenant


class TestReconstruction:
    async def test_run_timeline_survives_the_loss_of_every_operational_row(self, backend):
        """The audit log alone tells what happened to an incident (ADR 0007)."""
        from src.l1_agent.db import models
        from src.l1_agent.demo import DEMO_INCIDENTS
        from src.l1_agent.models.incident import Incident
        from tests.conftest import make_service

        tenant = await backend.new_tenant()
        service = make_service(backend.db)
        incident = Incident.from_servicenow(DEMO_INCIDENTS[1])
        ingested = await service.ingest(tenant, incident, Actor.system("intake"))
        await service.work_once([tenant])
        before = await events_for(backend.db, tenant, correlation_id=str(ingested.run_id))

        async with backend.db.tenant(tenant) as conn:
            for table in (
                models.jobs,
                models.run_steps,
                models.events,
                models.runs,
                models.incidents,
            ):
                await conn.execute(sa.delete(table).where(table.c.tenant_id == tenant))

        after = await events_for(backend.db, tenant, correlation_id=str(ingested.run_id))
        assert after == before
        by_type = {e["event_type"]: e["payload"] for e in after}
        assert by_type["incident.received"]["number"] == incident.number
        assert by_type["run.sop_matched"]["sop_id"] == "SOP-MQ-001"
        assert by_type["run.sop_matched"]["sop_version"]
        assert by_type["run.concluded"]["outcome"] == "resolved"
        assert by_type["run.concluded"]["verified_by"] == "step-verify"
        steps = [e["payload"] for e in after if e["event_type"] == "step.completed"]
        assert [s["step_id"] for s in steps] == [
            s["step_id"] for s in by_type["run.concluded"]["steps"]
        ]
        assert all(len(s["evidence_sha256"]) == 64 for s in steps)
        assert (await verify(backend.db, tenant)).ok
