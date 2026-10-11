"""Durable runs: intake, the job queue and the worker (ADR 0004).

Intake inserts the incident, its run, the job that will execute it and the
audit event in one transaction. A unique constraint decides which of two
deliveries of the same ticket wins; the loser creates nothing.

A worker claims a job with a lease. If the worker dies the lease expires and
another worker takes the job, resuming from the run's journal: the recorded
SOP choice and completed step results are reused, not repeated.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.db.models import events, incidents, jobs, run_steps, runs
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.journal import StoredMatch
from src.l1_agent.models.evidence import ExecutionOutcome, ExecutionSummary, StepResult
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, SOPStep
from src.l1_agent.sop import store as sop_store
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("runs")

SOURCE_SERVICENOW = "servicenow"
WORKER = Actor.system("worker")

# The run a coroutine is working on, so events published by the engine land in
# the right tenant without the engine knowing about tenants.
_current_run: ContextVar[Optional[Tuple[uuid.UUID, uuid.UUID]]] = ContextVar(
    "l1_current_run", default=None
)


def insert_ignoring_conflict(conn: AsyncConnection, table: sa.Table):
    """INSERT ... ON CONFLICT DO NOTHING for the connection's dialect."""
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table).on_conflict_do_nothing()


def build_record(incident: Incident, summary: ExecutionSummary) -> Dict[str, Any]:
    """The run as the dashboard and the API show it."""
    if not summary.sop_id:
        reason_code = "no_sop"
    elif not summary.step_results and summary.escalation_reason.startswith("Low confidence"):
        reason_code = "low_confidence"
    else:
        reason_code = "sop_execution"
    return {
        "incident_number": summary.incident_number,
        "short_description": incident.short_description,
        "category": incident.category,
        "subcategory": incident.subcategory,
        "assignment_group": incident.assignment_group,
        "priority": incident.priority,
        "cmdb_ci": incident.cmdb_ci,
        "sop_id": summary.sop_id,
        "sop_title": summary.sop_title,
        "outcome": summary.outcome_label,
        "verified_by": summary.verified_by,
        "escalation_reason": summary.escalation_reason,
        "reason_code": reason_code,
        "duration_ms": summary.total_duration_ms,
        "started_at": summary.started_at,
        "completed_at": summary.completed_at or utcnow().isoformat(),
        "step_count": len(summary.step_results),
        "step_results": [r.to_audit_dict() for r in summary.step_results],
    }


# ── Events (the outbox the dashboard stream reads) ────────────────────


class DbEventBus:
    """`publish()` like the in-memory bus, but durable and visible to every process."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def publish(self, event: dict) -> None:
        current = _current_run.get()
        if current is None:
            return
        tenant_id, run_id = current
        try:
            async with self._db.tenant(tenant_id) as conn:
                await write_event(conn, tenant_id, run_id, event)
        except Exception as exc:  # a lost progress event must not stop a run
            logger.warning("Could not record event %s: %s", event.get("type"), type(exc).__name__)


async def write_event(
    conn: AsyncConnection, tenant_id: uuid.UUID, run_id: Optional[uuid.UUID], event: dict
) -> None:
    now = utcnow()
    payload = {**event, "ts": event.get("ts") or now.isoformat()}
    await conn.execute(
        sa.insert(events).values(
            tenant_id=tenant_id,
            run_id=run_id,
            type=str(event.get("type", "")),
            payload=payload,
            created_at=now,
        )
    )


async def events_after(
    db: Database, tenant_id: uuid.UUID, after_id: int, limit: int = 200
) -> List[Tuple[int, dict]]:
    async with db.tenant(tenant_id) as conn:
        rows = await conn.execute(
            sa.select(events.c.id, events.c.payload)
            .where(events.c.tenant_id == tenant_id, events.c.id > after_id)
            .order_by(events.c.id)
            .limit(limit)
        )
        return [(r.id, r.payload) for r in rows]


async def latest_event_id(db: Database, tenant_id: uuid.UUID) -> int:
    async with db.tenant(tenant_id) as conn:
        found = await conn.execute(
            sa.select(sa.func.max(events.c.id)).where(events.c.tenant_id == tenant_id)
        )
        return found.scalar() or 0


# ── Journal ───────────────────────────────────────────────────────────


class DbJournal:
    """The run's durable record. Each write commits with its audit event."""

    def __init__(
        self,
        db: Database,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        match: Optional[StoredMatch],
        completed: Dict[str, StepResult],
        steps_seen: int,
    ) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._run_id = run_id
        self._match = match
        self._completed = completed
        self._seq = steps_seen
        self._resumed = bool(match or steps_seen)

    @classmethod
    async def load(cls, db: Database, tenant_id: uuid.UUID, run_id: uuid.UUID) -> "DbJournal":
        async with db.tenant(tenant_id) as conn:
            run = (
                await conn.execute(
                    sa.select(runs).where(runs.c.tenant_id == tenant_id, runs.c.id == run_id)
                )
            ).one()
            rows = (
                await conn.execute(
                    sa.select(run_steps).where(
                        run_steps.c.tenant_id == tenant_id, run_steps.c.run_id == run_id
                    )
                )
            ).all()
        match = None
        if run.sop_id:
            match = StoredMatch(
                run.sop_id,
                run.sop_version,
                float(run.confidence or 0.0),
                run.match_rationale,
                run.sop_hash,
            )
        completed = {
            r.step_id: StepResult.from_dict(r.result)
            for r in rows
            if r.status == "completed" and r.result
        }
        return cls(db, tenant_id, run_id, match, completed, len(rows))

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    @property
    def run_id(self) -> uuid.UUID:
        return self._run_id

    @property
    def resumed(self) -> bool:
        return self._resumed

    @property
    def match(self) -> Optional[StoredMatch]:
        return self._match

    def completed(self, step_id: str) -> Optional[StepResult]:
        return self._completed.get(step_id)

    async def record_match(self, sop: SOP, confidence: float, rationale: str) -> None:
        async with self._db.tenant(self._tenant_id) as conn:
            await conn.execute(
                sa.update(runs)
                .where(runs.c.tenant_id == self._tenant_id, runs.c.id == self._run_id)
                .values(
                    sop_id=sop.sop_id,
                    sop_version=str(sop.version),
                    sop_hash=sop.content_hash,
                    confidence=confidence,
                    match_rationale=rationale,
                    state="executing",
                )
            )
            await audit.append(
                conn,
                self._tenant_id,
                WORKER,
                "run.sop_matched",
                {
                    "sop_id": sop.sop_id,
                    "sop_version": str(sop.version),
                    "sop_sha256": sop.content_hash,
                    "confidence": round(confidence, 4),
                    "rationale": rationale,
                },
                correlation_id=str(self._run_id),
            )

    async def step_started(self, step: SOPStep) -> None:
        self._seq += 1
        async with self._db.tenant(self._tenant_id) as conn:
            # A step that was started but not finished before a crash is started again.
            await conn.execute(
                insert_ignoring_conflict(conn, run_steps).values(
                    id=uuid.uuid4(),
                    tenant_id=self._tenant_id,
                    run_id=self._run_id,
                    seq=self._seq,
                    step_id=step.step_id,
                    status="started",
                    started_at=utcnow(),
                )
            )
            await audit.append(
                conn,
                self._tenant_id,
                WORKER,
                "step.started",
                {
                    "step_id": step.step_id,
                    "step_type": step.step_type,
                    "parameters_sha256": audit.digest(step.parameters),
                },
                correlation_id=str(self._run_id),
            )

    async def step_completed(self, step: SOPStep, result: StepResult) -> None:
        async with self._db.tenant(self._tenant_id) as conn:
            await conn.execute(
                sa.update(run_steps)
                .where(
                    run_steps.c.tenant_id == self._tenant_id,
                    run_steps.c.run_id == self._run_id,
                    run_steps.c.step_id == step.step_id,
                )
                .values(status="completed", result=result.to_dict(), completed_at=utcnow())
            )
            await audit.append(
                conn,
                self._tenant_id,
                WORKER,
                "step.completed",
                {
                    "step_id": step.step_id,
                    "step_type": step.step_type,
                    "status": result.status.value,
                    "tool": result.tool_called,
                    "output_summary": result.output_summary[:300],
                    "evidence_sha256": audit.digest(result.evidence),
                    "duration_ms": round(result.duration_ms, 1),
                },
                correlation_id=str(self._run_id),
            )
        self._completed[step.step_id] = result


# ── Service ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    run_id: Optional[uuid.UUID] = None


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    run_id: uuid.UUID
    attempts: int
    max_attempts: int


class RunService:
    def __init__(
        self,
        db: Database,
        processor: IncidentProcessor,
        *,
        worker_id: str = "",
        visibility_timeout_seconds: int = 300,
        max_attempts: int = 3,
        sops_from_store: bool = False,
    ) -> None:
        self._db = db
        self._processor = processor
        # True in the service: each run matches against the tenant's published versions.
        # False leaves the processor with the SOP list it was built with (unit tests).
        self._sops_from_store = sops_from_store
        self._worker_id = (
            worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        )
        self._lease = timedelta(seconds=visibility_timeout_seconds)
        self._max_attempts = max_attempts

    # ── Intake ────────────────────────────────────────────────────────

    async def ingest(
        self,
        tenant_id: uuid.UUID,
        incident: Incident,
        actor: Actor,
        source: str = SOURCE_SERVICENOW,
    ) -> IngestResult:
        """Record an incident and queue its run, once. A repeat delivery does nothing."""
        now = utcnow()
        async with self._db.tenant(tenant_id) as conn:
            incident_id = uuid.uuid4()
            inserted = await conn.execute(
                insert_ignoring_conflict(conn, incidents)
                .values(
                    id=incident_id,
                    tenant_id=tenant_id,
                    source=source,
                    external_id=incident.sys_id,
                    number=incident.number,
                    payload=incident.to_dict(),
                    received_at=now,
                )
                .returning(incidents.c.id)
            )
            if inserted.first() is None:
                metrics.increment("incidents.duplicates")
                return IngestResult(False)

            run_id = uuid.uuid4()
            await conn.execute(
                sa.insert(runs).values(
                    id=run_id,
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    generation=1,
                    number=incident.number,
                    priority=incident.priority,
                    state="received",
                    created_at=now,
                )
            )
            await conn.execute(
                sa.insert(jobs).values(
                    tenant_id=tenant_id,
                    kind="run",
                    run_id=run_id,
                    status="queued",
                    max_attempts=self._max_attempts,
                    available_at=now,
                    created_at=now,
                )
            )
            await audit.append(
                conn,
                tenant_id,
                actor,
                "incident.received",
                {
                    "number": incident.number,
                    "source": source,
                    "external_id": incident.sys_id,
                    "priority": incident.priority,
                    "payload_sha256": audit.digest(incident.to_dict()),
                },
                correlation_id=str(run_id),
            )
            await write_event(
                conn,
                tenant_id,
                run_id,
                {
                    "type": "incident_received",
                    "incident_number": incident.number,
                    "short_description": incident.short_description,
                    "priority": incident.priority,
                    "category": incident.category,
                },
            )
        metrics.increment("incidents.received")
        return IngestResult(True, run_id)

    # ── Queue ─────────────────────────────────────────────────────────

    async def claim(self, tenant_id: uuid.UUID) -> Optional[ClaimedJob]:
        """Take the oldest job that is waiting or whose lease has expired."""
        now = utcnow()
        candidate = (
            sa.select(jobs.c.id)
            .where(
                jobs.c.tenant_id == tenant_id,
                sa.or_(
                    sa.and_(jobs.c.status == "queued", jobs.c.available_at <= now),
                    sa.and_(jobs.c.status == "running", jobs.c.locked_until < now),
                ),
            )
            .order_by(jobs.c.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        async with self._db.tenant(tenant_id) as conn:
            row = (
                await conn.execute(
                    sa.update(jobs)
                    .where(jobs.c.id == candidate)
                    .values(
                        status="running",
                        attempts=jobs.c.attempts + 1,
                        locked_until=now + self._lease,
                        locked_by=self._worker_id,
                    )
                    .returning(jobs.c.id, jobs.c.run_id, jobs.c.attempts, jobs.c.max_attempts)
                )
            ).first()
        return ClaimedJob(row.id, row.run_id, row.attempts, row.max_attempts) if row else None

    async def _heartbeat(self, tenant_id: uuid.UUID, job_id: int) -> None:
        """Keep the lease while the run is making progress."""
        interval = max(self._lease.total_seconds() / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self._db.tenant(tenant_id) as conn:
                    await conn.execute(
                        sa.update(jobs)
                        .where(jobs.c.id == job_id, jobs.c.locked_by == self._worker_id)
                        .values(locked_until=utcnow() + self._lease)
                    )
            except Exception as exc:
                logger.warning("Lease renewal failed for job %s: %s", job_id, type(exc).__name__)

    # ── Execution ─────────────────────────────────────────────────────

    async def _load(
        self, tenant_id: uuid.UUID, run_id: uuid.UUID
    ) -> Tuple[Optional[str], Incident]:
        async with self._db.tenant(tenant_id) as conn:
            row = (
                await conn.execute(
                    sa.select(runs.c.outcome, incidents.c.payload)
                    .join(incidents, incidents.c.id == runs.c.incident_id)
                    .where(runs.c.tenant_id == tenant_id, runs.c.id == run_id)
                )
            ).one()
        return row.outcome, Incident(**row.payload)

    async def execute(self, tenant_id: uuid.UUID, job: ClaimedJob) -> None:
        """Run a claimed job to a terminal outcome and record it."""
        outcome, incident = await self._load(tenant_id, job.run_id)
        if outcome is not None:
            await self._finish_job(tenant_id, job.id, "done")
            return
        if job.attempts > job.max_attempts:
            await self._abandon(tenant_id, job, incident)
            return

        journal = await DbJournal.load(self._db, tenant_id, job.run_id)
        if journal.resumed:
            metrics.increment("runs.resumed")
            logger.info("Resuming run %s for %s", job.run_id, incident.number)
        sops = await self._published(tenant_id, journal) if self._sops_from_store else None
        token = _current_run.set((tenant_id, job.run_id))
        heartbeat = asyncio.create_task(self._heartbeat(tenant_id, job.id))
        try:
            summary = await self._processor.run_once(incident, journal, sops)
        finally:
            heartbeat.cancel()
            _current_run.reset(token)
        await self._conclude(tenant_id, job, incident, summary, "done")

    async def _published(self, tenant_id: uuid.UUID, journal: DbJournal) -> List[SOP]:
        """The tenant's published SOPs, plus the exact version a resumed run started with."""
        sops = await sop_store.published(self._db, tenant_id)
        stored = journal.match
        if stored and not any(
            s.sop_id == stored.sop_id and s.content_hash == stored.sop_hash for s in sops
        ):
            earlier = await sop_store.version_for_resume(
                self._db, tenant_id, stored.sop_id, stored.sop_version, stored.sop_hash
            )
            if earlier is not None:
                sops.append(earlier)
        return sops

    async def _abandon(self, tenant_id: uuid.UUID, job: ClaimedJob, incident: Incident) -> None:
        """A run that keeps dying is stopped and given to a person."""
        summary = ExecutionSummary(
            incident_number=incident.number,
            sop_id="",
            sop_title="",
            outcome=ExecutionOutcome.NEEDS_HUMAN,
            escalation_reason=(
                f"The run did not finish in {job.max_attempts} attempts and was stopped"
            ),
        )
        try:
            await self._processor.hand_to_human(incident, summary)
        except Exception as exc:
            logger.error("Could not route %s to a person: %s", incident.number, exc)
        await self._conclude(tenant_id, job, incident, summary, "dead")

    async def _conclude(
        self,
        tenant_id: uuid.UUID,
        job: ClaimedJob,
        incident: Incident,
        summary: ExecutionSummary,
        job_status: str,
    ) -> None:
        record = build_record(incident, summary)
        async with self._db.tenant(tenant_id) as conn:
            updated = await conn.execute(
                sa.update(runs)
                .where(
                    runs.c.tenant_id == tenant_id,
                    runs.c.id == job.run_id,
                    runs.c.outcome.is_(None),  # the first conclusion stands
                )
                .values(
                    outcome=summary.outcome_label,
                    state=summary.outcome_label,
                    sop_id=summary.sop_id,
                    record=record,
                    completed_at=utcnow(),
                )
            )
            await conn.execute(
                sa.update(jobs)
                .where(jobs.c.id == job.id)
                .values(status=job_status, locked_until=None)
            )
            if updated.rowcount:
                await audit.append(
                    conn,
                    tenant_id,
                    WORKER,
                    "run.concluded",
                    {
                        "number": incident.number,
                        "outcome": summary.outcome_label,
                        "sop_id": summary.sop_id,
                        "verified_by": summary.verified_by,
                        "reason": summary.escalation_reason,
                        "steps": [
                            {"step_id": r.step_id, "status": r.status.value}
                            for r in summary.step_results
                        ],
                        "attempts": job.attempts,
                    },
                    correlation_id=str(job.run_id),
                )

    async def _finish_job(self, tenant_id: uuid.UUID, job_id: int, status: str) -> None:
        async with self._db.tenant(tenant_id) as conn:
            await conn.execute(
                sa.update(jobs).where(jobs.c.id == job_id).values(status=status, locked_until=None)
            )

    async def _release(self, tenant_id: uuid.UUID, job: ClaimedJob, error: str) -> None:
        """Put a job back after an infrastructure error, with a delay."""
        delay = timedelta(seconds=min(60, 2**job.attempts))
        async with self._db.tenant(tenant_id) as conn:
            await conn.execute(
                sa.update(jobs)
                .where(jobs.c.id == job.id)
                .values(
                    status="queued",
                    available_at=utcnow() + delay,
                    locked_until=None,
                    last_error=error[:500],
                )
            )

    # ── Worker loop ───────────────────────────────────────────────────

    async def work_once(self, tenant_ids: Optional[List[uuid.UUID]] = None) -> bool:
        """Claim and run at most one job per tenant. Returns True if any ran."""
        worked = False
        for tenant_id in tenant_ids or await self._db.active_tenants():
            job = await self.claim(tenant_id)
            if job is None:
                continue
            worked = True
            try:
                await self.execute(tenant_id, job)
            except Exception as exc:
                logger.error("Job %s failed outside the engine: %s", job.id, exc)
                metrics.increment("jobs.infrastructure_errors")
                try:
                    await self._release(tenant_id, job, f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass  # the lease will expire and the job will be claimed again
        return worked

    async def run_worker(self, stop: asyncio.Event, poll_interval: float = 1.0) -> None:
        logger.info("Worker %s started", self._worker_id)
        while not stop.is_set():
            try:
                worked = await self.work_once()
            except Exception as exc:
                logger.error("Worker loop error: %s", exc)
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    pass
        logger.info("Worker %s stopped", self._worker_id)


# ── Reads for the dashboard and the API ───────────────────────────────

RECENT_RUNS = 1000


async def recent_records(db: Database, tenant_id: uuid.UUID) -> List[dict]:
    """Records of the most recent concluded runs, oldest first."""
    async with db.tenant(tenant_id) as conn:
        rows = await conn.execute(
            sa.select(runs.c.record)
            .where(runs.c.tenant_id == tenant_id, runs.c.record.is_not(None))
            .order_by(runs.c.created_at.desc())
            .limit(RECENT_RUNS)
        )
        return [r.record for r in rows][::-1]


async def open_run_count(db: Database, tenant_id: uuid.UUID) -> int:
    async with db.tenant(tenant_id) as conn:
        found = await conn.execute(
            sa.select(sa.func.count())
            .select_from(runs)
            .where(runs.c.tenant_id == tenant_id, runs.c.outcome.is_(None))
        )
        return found.scalar() or 0


async def record_by_number(db: Database, tenant_id: uuid.UUID, number: str) -> Optional[dict]:
    async with db.tenant(tenant_id) as conn:
        row = (
            await conn.execute(
                sa.select(runs.c.id, runs.c.record, runs.c.sop_version, runs.c.sop_hash)
                .where(
                    runs.c.tenant_id == tenant_id,
                    runs.c.number == number,
                    runs.c.record.is_not(None),
                )
                .order_by(runs.c.created_at.desc())
                .limit(1)
            )
        ).first()
    if row is None:
        return None
    return {
        **row.record,
        "run_id": str(row.id),
        "sop_version": row.sop_version,
        "sop_hash": row.sop_hash,
    }
