"""Exit criterion 5 with a real process: a worker is killed mid-run and another resumes.

test_runs_durable.py covers the same behaviour in one process with a simulated
crash. Here the worker is a separate operating-system process that exits
without running any handler, and a second process picks the run up.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from src.l1_agent.audit.log import Actor, events_for, verify
from src.l1_agent.db.models import jobs, run_steps
from src.l1_agent.demo import DEMO_INCIDENTS
from src.l1_agent.models.incident import Incident
from src.l1_agent.service.runs import record_by_number
from tests.conftest import make_service
from tests.db.worker_child import KILLED

RESOLVING = Incident.from_servicenow(DEMO_INCIDENTS[1])
ROOT = Path(__file__).resolve().parents[2]


def run_child(url: str, tenant, log: Path, die_on: int) -> int:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    done = subprocess.run(
        [sys.executable, "-m", "tests.db.worker_child", url, str(tenant), str(log), str(die_on)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if done.returncode not in (0, KILLED):
        raise AssertionError(f"worker exited {done.returncode}: {done.stderr[-2000:]}")
    return done.returncode


@pytest.mark.parametrize("die_on", [2, 3])
async def test_killed_worker_is_replaced_and_completed_steps_are_not_repeated(
    backend, tmp_path, die_on
):
    tenant = await backend.new_tenant()
    url = backend.db.engine.url.render_as_string(hide_password=False)
    ingested = await make_service(backend.db).ingest(tenant, RESOLVING, Actor.system("test"))

    # Reference: the calls an uninterrupted run makes, in another tenant.
    reference_tenant = await backend.new_tenant()
    await make_service(backend.db).ingest(reference_tenant, RESOLVING, Actor.system("test"))
    clean_log = tmp_path / "clean.log"
    assert await asyncio.to_thread(run_child, url, reference_tenant, clean_log, 0) == 0
    clean_calls = clean_log.read_text().splitlines()
    assert len(clean_calls) > die_on

    log = tmp_path / "calls.log"
    assert await asyncio.to_thread(run_child, url, tenant, log, die_on) == KILLED

    assert await record_by_number(backend.db, tenant, RESOLVING.number) is None
    async with backend.db.tenant(tenant) as conn:
        job = (await conn.execute(sa.select(jobs).where(jobs.c.tenant_id == tenant))).one()
        finished = (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(run_steps)
                .where(run_steps.c.tenant_id == tenant, run_steps.c.status == "completed")
            )
        ).scalar()
    assert job.status == "running", "the dead worker still appears to hold the job"
    assert finished >= die_on - 1, "steps finished before the kill were committed"

    time.sleep(1.2)  # the dead worker's one-second lease runs out
    assert await asyncio.to_thread(run_child, url, tenant, log, 0) == 0

    record = await record_by_number(backend.db, tenant, RESOLVING.number)
    assert record["outcome"] == "resolved" and record["verified_by"] == "step-verify"
    calls = log.read_text().splitlines()
    # One call more than a clean run: the one in flight when the process died.
    assert len(calls) == len(clean_calls) + 1
    assert calls[:die_on] == clean_calls[:die_on]
    assert calls[die_on:] == clean_calls[die_on - 1 :], "resumed at the interrupted call"
    timeline = await events_for(backend.db, tenant, correlation_id=str(ingested.run_id))
    assert timeline[-1]["payload"]["attempts"] == 2
    assert (await verify(backend.db, tenant)).ok
