"""Intake, the job queue and resuming a run after a worker dies.

Phase 2 exit criteria 1 (one run for one incident, whoever delivers it) and
5 (a restarted worker resumes without repeating a completed step).
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
import sqlalchemy as sa

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.audit.log import Actor, events_for, verify
from src.l1_agent.db.models import incidents, jobs, run_steps, runs
from src.l1_agent.demo import DEMO_INCIDENTS, MockServiceNowClient, build_mock_adapters
from src.l1_agent.models.incident import Incident
from src.l1_agent.service.runs import record_by_number
from tests.conftest import make_service

INTAKE = Actor.system("test-intake")
RESOLVING = Incident.from_servicenow(DEMO_INCIDENTS[1])  # ORDER.REQUEST: verified resolution
ESCALATING = Incident.from_servicenow(DEMO_INCIDENTS[0])  # PAYMENT.REQUEST: still backed up
NO_SOP = Incident.from_servicenow(DEMO_INCIDENTS[4])  # certificate: nothing matches


async def count(db, tenant_id, table) -> int:
    async with db.tenant(tenant_id) as conn:
        return (
            await conn.execute(
                sa.select(sa.func.count()).select_from(table).where(table.c.tenant_id == tenant_id)
            )
        ).scalar()


class SimulatedCrash(BaseException):
    """Not an Exception: nothing in the engine catches it, like a killed process."""


class CountingAdapter(BaseAdapter):
    """Counts calls to the adapter it wraps, and can die on a chosen call."""

    def __init__(self, inner: BaseAdapter, calls: Counter, name: str, crash_on: set) -> None:
        self._inner = inner
        self._calls = calls
        self._name = name
        self._crash_on = crash_on

    @property
    def adapter_name(self) -> str:
        return self._inner.adapter_name

    async def execute(self, params: dict) -> AdapterResult:
        self._calls[self._name] += 1
        total = sum(self._calls.values())
        if total in self._crash_on:
            self._crash_on.discard(total)
            raise SimulatedCrash(f"killed during call {total}")
        return await self._inner.execute(params)

    async def health_check(self) -> bool:
        return True


def counting_adapters(calls: Counter, crash_on: set) -> dict:
    return {
        name: CountingAdapter(adapter, calls, name, crash_on)
        for name, adapter in build_mock_adapters().items()
    }


class TestIntake:
    async def test_incident_becomes_one_run_with_a_job_and_an_audit_event(self, backend):
        tenant = await backend.new_tenant()
        service = make_service(backend.db)

        result = await service.ingest(tenant, RESOLVING, INTAKE)

        assert result.accepted and result.run_id
        assert await count(backend.db, tenant, incidents) == 1
        assert await count(backend.db, tenant, runs) == 1
        assert await count(backend.db, tenant, jobs) == 1
        timeline = await events_for(backend.db, tenant, correlation_id=str(result.run_id))
        assert [e["event_type"] for e in timeline] == ["incident.received"]
        assert timeline[0]["actor"] == {"type": "system", "id": "test-intake"}

    async def test_repeat_delivery_creates_nothing(self, backend):
        tenant = await backend.new_tenant()
        service = make_service(backend.db)

        first = await service.ingest(tenant, RESOLVING, INTAKE)
        again = await service.ingest(tenant, RESOLVING, INTAKE)

        assert first.accepted and not again.accepted and again.run_id is None
        assert await count(backend.db, tenant, runs) == 1
        assert await count(backend.db, tenant, jobs) == 1

    async def test_same_ticket_in_two_tenants_is_two_runs(self, backend):
        one, two = await backend.new_tenant(), await backend.new_tenant()
        service = make_service(backend.db)

        assert (await service.ingest(one, RESOLVING, INTAKE)).accepted
        assert (await service.ingest(two, RESOLVING, INTAKE)).accepted

    async def test_concurrent_deliveries_from_many_processes_make_one_run(self, pg):
        """Exit criterion 1. Webhook and pollers race; the constraint picks one."""
        tenant = await pg.new_tenant()
        services = [make_service(pg.db, worker_id=f"w{i}") for i in range(12)]

        results = await asyncio.gather(*(s.ingest(tenant, RESOLVING, INTAKE) for s in services))

        assert sum(r.accepted for r in results) == 1
        assert await count(pg.db, tenant, incidents) == 1
        assert await count(pg.db, tenant, runs) == 1
        assert await count(pg.db, tenant, jobs) == 1

    async def test_a_second_open_run_for_an_incident_is_refused_by_the_database(self, backend):
        tenant = await backend.new_tenant()
        service = make_service(backend.db)
        await service.ingest(tenant, RESOLVING, INTAKE)

        with pytest.raises(sa.exc.IntegrityError):
            async with backend.db.tenant(tenant) as conn:
                run = (await conn.execute(sa.select(runs).where(runs.c.tenant_id == tenant))).one()
                await conn.execute(
                    sa.insert(runs).values(
                        id=__import__("uuid").uuid4(),
                        tenant_id=tenant,
                        incident_id=run.incident_id,
                        generation=2,
                        number=run.number,
                        state="received",
                        created_at=run.created_at,
                    )
                )


class TestQueue:
    async def test_two_workers_claiming_at_once_get_one_job_between_them(self, pg):
        """Exit criterion 1, second half: one incident is executed by one worker."""
        tenant = await pg.new_tenant()
        workers = [make_service(pg.db, worker_id=f"w{i}") for i in range(8)]
        await workers[0].ingest(tenant, RESOLVING, INTAKE)

        claims = await asyncio.gather(*(w.claim(tenant) for w in workers))

        assert sum(c is not None for c in claims) == 1

    async def test_two_workers_run_two_incidents_once_each(self, pg):
        tenant = await pg.new_tenant()
        calls: Counter = Counter()
        snow = MockServiceNowClient()
        workers = [
            make_service(pg.db, adapters=counting_adapters(calls, set()), snow=snow, worker_id=w)
            for w in ("a", "b")
        ]
        await workers[0].ingest(tenant, RESOLVING, INTAKE)
        await workers[0].ingest(tenant, ESCALATING, INTAKE)

        for _ in range(3):
            await asyncio.gather(*(w.work_once([tenant]) for w in workers))

        async with pg.db.tenant(tenant) as conn:
            outcomes = sorted(r.outcome for r in await conn.execute(sa.select(runs.c.outcome)))
            statuses = [r.status for r in await conn.execute(sa.select(jobs.c.status))]
        assert outcomes == ["escalated", "resolved"]
        assert statuses == ["done", "done"]
        resolved = [u for u in snow.updates if u["fields"].get("state") == "6"]
        assert len(resolved) == 1, "the incident was resolved in the ITSM exactly once"

    async def test_run_reaches_its_outcome_and_is_recorded(self, backend):
        tenant = await backend.new_tenant()
        service = make_service(backend.db)
        ingested = await service.ingest(tenant, RESOLVING, INTAKE)

        assert await service.work_once([tenant]) is True
        assert await service.work_once([tenant]) is False  # nothing left

        record = await record_by_number(backend.db, tenant, RESOLVING.number)
        assert record["outcome"] == "resolved"
        assert record["verified_by"] == "step-verify"
        timeline = await events_for(backend.db, tenant, correlation_id=str(ingested.run_id))
        types = [e["event_type"] for e in timeline]
        assert types[0] == "incident.received"
        assert types[1] == "run.sop_matched"
        assert types[-1] == "run.concluded"
        assert types.count("step.started") == types.count("step.completed") == record["step_count"]
        assert timeline[-1]["payload"]["outcome"] == "resolved"
        assert (await verify(backend.db, tenant)).ok

    async def test_unmatched_incident_is_recorded_as_escalated(self, backend):
        tenant = await backend.new_tenant()
        service = make_service(backend.db)
        await service.ingest(tenant, NO_SOP, INTAKE)
        await service.work_once([tenant])

        record = await record_by_number(backend.db, tenant, NO_SOP.number)
        assert record["outcome"] == "escalated"
        assert record["reason_code"] in ("no_sop", "low_confidence")
        assert record["step_count"] == 0


class TestResumeAfterWorkerDeath:
    @pytest.mark.parametrize("crash_at_call", [1, 2, 3])
    async def test_restarted_worker_does_not_repeat_completed_steps(self, backend, crash_at_call):
        """Exit criterion 5."""
        tenant = await backend.new_tenant()
        calls: Counter = Counter()
        snow = MockServiceNowClient()
        adapters = counting_adapters(calls, {crash_at_call})
        # Reference: how many adapter calls an uninterrupted run makes.
        clean_calls: Counter = Counter()
        reference_tenant = await backend.new_tenant()
        reference = make_service(backend.db, adapters=counting_adapters(clean_calls, set()))
        await reference.ingest(reference_tenant, RESOLVING, INTAKE)
        await reference.work_once([reference_tenant])
        assert sum(clean_calls.values()) >= 3, "the scenario needs at least three adapter calls"

        dying = make_service(
            backend.db, adapters=adapters, snow=snow, lease_seconds=0, worker_id="dying"
        )
        ingested = await dying.ingest(tenant, RESOLVING, INTAKE)
        with pytest.raises(SimulatedCrash):
            await dying.work_once([tenant])

        async with backend.db.tenant(tenant) as conn:
            mid = (await conn.execute(sa.select(runs).where(runs.c.tenant_id == tenant))).one()
            done_before = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(run_steps)
                    .where(run_steps.c.tenant_id == tenant, run_steps.c.status == "completed")
                )
            ).scalar()
        assert mid.outcome is None, "the dead worker left the run open"
        assert mid.sop_id == "SOP-MQ-001", "the SOP choice was committed before the crash"

        await asyncio.sleep(0.05)  # the dead worker's lease runs out
        survivor = make_service(backend.db, adapters=adapters, snow=snow, worker_id="survivor")
        assert await survivor.work_once([tenant]) is True

        record = await record_by_number(backend.db, tenant, RESOLVING.number)
        assert record["outcome"] == "resolved"
        assert record["verified_by"] == "step-verify"
        # Exactly one extra call: the one that was in flight when the worker died.
        assert sum(calls.values()) == sum(clean_calls.values()) + 1
        timeline = await events_for(backend.db, tenant, correlation_id=str(ingested.run_id))
        completed = [
            e["payload"]["step_id"] for e in timeline if e["event_type"] == "step.completed"
        ]
        assert len(completed) == len(set(completed)), "no step completed twice"
        assert len(completed) == record["step_count"]
        assert len(completed) >= done_before
        assert [e["event_type"] for e in timeline].count("run.sop_matched") == 1
        assert timeline[-1]["payload"]["attempts"] == 2
        assert (await verify(backend.db, tenant)).ok

    async def test_resumed_run_does_not_repeat_the_opening_work_notes(self, backend):
        tenant = await backend.new_tenant()
        snow = MockServiceNowClient()
        adapters = counting_adapters(Counter(), {2})
        dying = make_service(backend.db, adapters=adapters, snow=snow, lease_seconds=0)
        await dying.ingest(tenant, RESOLVING, INTAKE)
        with pytest.raises(SimulatedCrash):
            await dying.work_once([tenant])
        await asyncio.sleep(0.05)
        await make_service(backend.db, adapters=adapters, snow=snow).work_once([tenant])

        notes = [n["note"] for n in snow.work_notes]
        assert sum("Incident received" in n for n in notes) == 1
        assert sum("SOP matched" in n for n in notes) == 1
        assert sum("Starting SOP" in n for n in notes) == 1

    async def test_a_run_that_keeps_dying_is_stopped_and_given_to_a_person(self, backend):
        tenant = await backend.new_tenant()
        snow = MockServiceNowClient()
        always = set(range(1, 50))
        adapters = counting_adapters(Counter(), always)
        service = make_service(
            backend.db, adapters=adapters, snow=snow, lease_seconds=0, max_attempts=2
        )
        ingested = await service.ingest(tenant, RESOLVING, INTAKE)

        for _ in range(2):
            with pytest.raises(SimulatedCrash):
                await service.work_once([tenant])
            await asyncio.sleep(0.05)
        await service.work_once([tenant])  # third claim: over the limit

        record = await record_by_number(backend.db, tenant, RESOLVING.number)
        assert record["outcome"] == "needs_human"
        assert "did not finish in 2 attempts" in record["escalation_reason"]
        async with backend.db.tenant(tenant) as conn:
            status = (await conn.execute(sa.select(jobs.c.status))).scalar()
        assert status == "dead"
        assert any("assignment_group" in u["fields"] for u in snow.updates), "routed to a team"
        timeline = await events_for(backend.db, tenant, correlation_id=str(ingested.run_id))
        assert timeline[-1]["payload"]["outcome"] == "needs_human"

    async def test_a_finished_run_is_never_executed_again(self, backend):
        tenant = await backend.new_tenant()
        calls: Counter = Counter()
        service = make_service(backend.db, adapters=counting_adapters(calls, set()))
        await service.ingest(tenant, RESOLVING, INTAKE)
        await service.work_once([tenant])
        before = sum(calls.values())

        # Someone puts the finished job back on the queue.
        async with backend.db.tenant(tenant) as conn:
            await conn.execute(sa.update(jobs).values(status="queued"))
        await service.work_once([tenant])

        assert sum(calls.values()) == before
