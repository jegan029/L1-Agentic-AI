"""Actions end to end against the mocks (Phase 4 exit criteria 1, 3, 4 and 5).

Every test runs the real engine, the real policy and the real database (SQLite
and PostgreSQL). The scheduler and ServiceNow are mocks: nothing here shows how
a real Autosys or a real ServiceNow instance behaves.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.l1_agent.actions import store as action_store
from src.l1_agent.audit.log import Actor, verify
from src.l1_agent.config.settings import ActionSettings
from src.l1_agent.db.engine import utcnow
from src.l1_agent.db.models import actions, incidents, users
from tests.actions_support import (
    ADMIN,
    APPROVER,
    AUTHOR,
    FAILS_AGAIN,
    FORCE_START,
    RESTART_SOP,
    REVIEWER,
    T1_JOB,
    T2_JOB,
    make_world,
    reconstruct,
)
from tests.api.support import seed_action


async def set_columns(world, incident, **values) -> None:
    """Change an action row the way the application role can."""
    async with world.db.tenant(world.tenant) as conn:
        await conn.execute(
            sa.update(actions)
            .where(
                actions.c.tenant_id == world.tenant, actions.c.incident_number == incident.number
            )
            .values(**values)
        )


# ── Exit criterion 1: the scenario in each mode, rebuilt from the audit log ──


class TestScenarioInEachMode:
    async def test_shadow_records_what_it_would_do_and_changes_nothing(self, backend):
        world = await make_world(backend, autonomy="shadow")
        incident = await world.run()
        record, action = await world.record(incident), await world.action(incident)

        assert world.autosys.force_starts == [] and world.snow.changes == {}
        assert record["outcome"] == "escalated", "the job is still failed, so nothing is resolved"
        assert action["status"] == "shadowed" and action["verdict"] == "shadow"
        restart = next(s for s in record["step_results"] if s["step_id"] == "step-restart")
        assert restart["status"] == "skip" and "would have run" in restart["output_summary"]
        assert any("Shadow mode: would have run" in n["note"] for n in world.snow.work_notes)

        story = reconstruct(await world.events(incident))
        assert story["incident"] == incident.number and story["outcome"] == "escalated"
        assert story["sop"] == (RESTART_SOP, "1", world.sop.content_hash)
        assert story["action"] == {
            "capability": FORCE_START,
            "parameters": {"job_name": T1_JOB},
            "tier": "T1",
            "action_sha256": action["action_hash"],
            "verdict": "shadow",
            "reason_code": "autonomy",
        }
        assert "executed" not in story["action"] and story["suspended"] == 0
        assert story["steps"][-2:] == [("step-verify", "fail"), ("step-still-failed", "escalated")]

    async def test_shadow_resolves_when_the_symptom_is_already_gone(self, backend):
        """A shadow run is not forced to escalate: verification decides, as always."""
        world = await make_world(backend, autonomy="shadow")
        # Someone restarted the job by hand; the scheduler now shows it done.
        await world.autosys.force_start(T1_JOB)
        await world.autosys.execute({"job_name": T1_JOB})
        world.autosys.force_starts.clear()
        incident = await world.run()
        record = await world.record(incident)
        assert record["outcome"] == "resolved" and record["verified_by"] == "step-verify"
        assert world.autosys.force_starts == [] and await world.action(incident) is None

    async def test_recommend_posts_the_proposal_and_escalates(self, backend):
        world = await make_world(backend, autonomy="recommend")
        incident = await world.run()
        record, action = await world.record(incident), await world.action(incident)
        assert world.autosys.force_starts == [] and world.snow.changes == {}
        assert record["outcome"] == "escalated" and action["status"] == "recommended"
        assert "Recommended, not done" in record["escalation_reason"]
        notes = [n["note"] for n in world.snow.work_notes]
        assert any("Recommended action: autosys.job.force_start" in n for n in notes)
        assert reconstruct(await world.events(incident))["action"]["verdict"] == "recommend"

    async def test_approval_mode_waits_then_acts_on_approval(self, backend):
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()

        # Parked: no outcome, nothing done, a change record open and a request waiting.
        assert await world.record(incident) is None
        assert await world.run_state(incident) == ("awaiting_approval", None, "waiting")
        assert world.autosys.force_starts == []
        action = await world.action(incident)
        assert action["status"] == "pending" and action["tier"] == "T1"
        assert action["parameters"] == {"job_name": T1_JOB} and action["change_number"]
        change = world.snow.changes[action["change_number"]]
        assert change["correlation"] == action["action_hash"] and change["closed"] is None
        assert action["action_hash"] in change["description"]
        assert await world.work() is False, "a parked run is not picked up again"

        await world.decide(incident)
        assert await world.run_state(incident) == ("awaiting_approval", None, "queued")
        await world.work()

        record = await world.record(incident)
        assert record["outcome"] == "resolved" and record["verified_by"] == "step-verify"
        assert world.autosys.force_starts == [T1_JOB]
        assert change["closed"]["successful"] is True
        assert (await world.action(incident))["status"] == "verified"

        story = reconstruct(await world.events(incident))
        assert story["suspended"] == 1 and story["outcome"] == "resolved"
        assert story["action"] == {
            "capability": FORCE_START,
            "parameters": {"job_name": T1_JOB},
            "tier": "T1",
            "action_sha256": action["action_hash"],
            "verdict": "require_approval",
            "reason_code": "autonomy",
            "change": action["change_number"],
            "asked": ["ui"],
            "decision": "approved",
            "decided_by": f"user:{APPROVER.id}",
            "decided_on": action["action_hash"],
            "executed": {"job_name": T1_JOB},
            "authorised_by": f"user:{APPROVER.id}",
            "result": "verified",
            "change_closed_successful": True,
        }
        # Steps before the approval ran once; none was repeated when the run resumed.
        assert [s for s, _ in story["steps"]] == [
            "step-status",
            "step-is-failed",
            "step-logs",
            "step-restart",
            "step-verify",
            "step-resolve",
        ]
        order = story["types"]
        assert order.index("policy.decided") < order.index("change.opened")
        assert order.index("approval.decided") < order.index("action.executing")
        assert order.index("action.executing") < order.index("action.executed")
        assert order.index("action.verified") < order.index("run.concluded")

    async def test_automatic_mode_acts_without_a_person(self, backend):
        world = await make_world(backend)
        incident = await world.run()
        record, action = await world.record(incident), await world.action(incident)

        assert record["outcome"] == "resolved" and record["verified_by"] == "step-verify"
        assert world.autosys.force_starts == [T1_JOB]
        assert action["status"] == "verified" and action["approver"] is None
        assert action["verdict"] == "allow" and action["reason_code"] == "on_automatic_list"
        assert world.snow.changes[action["change_number"]]["closed"]["successful"] is True
        assert world.sleeps == [2], "the job read RUNNING first; the engine waited as the SOP says"

        story = reconstruct(await world.events(incident))
        assert story["suspended"] == 0 and story["outcome"] == "resolved"
        assert story["action"]["verdict"] == "allow"
        assert story["action"]["authorised_by"] == "policy" and "decision" not in story["action"]
        assert story["action"]["executed"] == {"job_name": T1_JOB}
        assert story["action"]["result"] == "verified"
        assert (await verify(world.db, world.tenant)).ok

    async def test_the_audit_log_and_the_run_record_tell_the_same_story(self, backend):
        """The record is what the dashboard shows; the audit log is the evidence. They agree."""
        for autonomy in ("shadow", "approval", "automatic"):
            world = await make_world(backend, autonomy=autonomy)
            incident = await world.run()
            if autonomy == "approval":
                await world.decide(incident)
                await world.work()
            record = await world.record(incident)
            story = reconstruct(await world.events(incident))
            assert story["outcome"] == record["outcome"], autonomy
            assert story["verified_by"] == record["verified_by"]
            assert story["steps"] == [(s["step_id"], s["status"]) for s in record["step_results"]]
            assert story["types"][0] == "incident.received"
            assert story["types"][-1] == "run.concluded"
            made = story["action"].get("executed")
            assert world.autosys.force_starts == ([made["job_name"]] if made else [])


# ── Exit criterion 3: approvals ───────────────────────────────────────


class TestApprovals:
    async def test_changed_parameters_invalidate_the_approval(self, backend):
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await world.decide(incident)
        # Between the approval and the execution the ticket is edited to name another job.
        async with world.db.tenant(world.tenant) as conn:
            payload = (
                await conn.execute(
                    sa.select(incidents.c.payload).where(incidents.c.number == incident.number)
                )
            ).scalar_one()
            changed = {
                k: v.replace(T1_JOB, "NIGHTLY_OTHER_LOAD") if isinstance(v, str) else v
                for k, v in payload.items()
            }
            await conn.execute(
                sa.update(incidents)
                .where(incidents.c.number == incident.number)
                .values(payload=changed)
            )
        await world.work()

        record, action = await world.record(incident), await world.action(incident)
        assert world.autosys.force_starts == [], "neither the old job nor the new one was started"
        assert record["outcome"] == "escalated" and action["status"] == "invalid"
        assert "its parameters changed" in record["escalation_reason"]
        assert action["parameters"] == {"job_name": T1_JOB}, (
            "the record still shows what was approved"
        )
        types = reconstruct(await world.events(incident))["types"]
        assert "action.invalidated" in types and "action.executing" not in types

    async def test_the_approver_must_have_seen_this_action(self, backend):
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        with pytest.raises(action_store.WrongState, match="not the one that was shown"):
            await world.decide(incident, seen_hash="0" * 64)
        with pytest.raises(action_store.WrongState):
            await world.decide(incident, seen_hash="")
        assert (await world.action(incident))["status"] == "pending"

    async def test_an_expired_request_cannot_be_approved(self, backend):
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await set_columns(world, incident, expires_at=utcnow() - timedelta(seconds=1))
        with pytest.raises(action_store.WrongState, match="expired"):
            await world.decide(incident)
        assert (await world.action(incident))["status"] == "expired"
        await world.work()
        record = await world.record(incident)
        assert record["outcome"] == "escalated" and world.autosys.force_starts == []
        assert "approval.expired" in reconstruct(await world.events(incident))["types"]

    async def test_a_request_nobody_answers_expires_and_the_run_escalates(self, backend):
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await set_columns(world, incident, expires_at=utcnow() - timedelta(seconds=1))
        await world.work()  # the worker's sweep expires it and wakes the run
        record = await world.record(incident)
        assert record["outcome"] == "escalated" and world.autosys.force_starts == []
        assert "expired with no decision" in record["escalation_reason"]

    async def test_an_approval_that_is_not_acted_on_in_time_is_void(self, backend):
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await world.decide(incident)
        await set_columns(world, incident, valid_until=utcnow() - timedelta(seconds=1))
        await world.work()
        record, action = await world.record(incident), await world.action(incident)
        assert record["outcome"] == "escalated" and action["status"] == "invalid"
        assert world.autosys.force_starts == []

    async def test_the_sop_author_cannot_approve_a_t2_action(self, backend):
        world = await make_world(backend)
        incident = await world.run(T2_JOB)
        action = await world.action(incident)
        assert action["tier"] == "T2" and action["status"] == "pending"
        assert action["sop_author"] == f"user:{AUTHOR.id}"
        assert action["reason_code"] == "tier_needs_approval", "T2 waits even in automatic mode"

        with pytest.raises(action_store.NotAllowed, match="author of the SOP"):
            await world.decide(incident, actor=AUTHOR)
        assert (await world.action(incident))["status"] == "pending"
        assert world.autosys.force_starts == []

        await world.decide(incident, actor=REVIEWER)  # anyone else with the permission may
        await world.work()
        assert (await world.record(incident))["outcome"] == "resolved"
        assert world.autosys.force_starts == [T2_JOB]

    async def test_the_two_person_rule_for_t2_is_also_a_database_constraint(self, backend):
        world = await make_world(backend)
        incident = await world.run(T2_JOB)
        author = f"user:{AUTHOR.id}"
        with pytest.raises(IntegrityError):
            await set_columns(world, incident, approver_id=author)
        action = await world.action(incident)
        with pytest.raises(IntegrityError):
            await set_columns(
                world,
                incident,
                status="approved",
                approver_id=author,
                approved_hash=action["action_hash"],
            )
        assert (await world.action(incident))["approver"] is None

    async def test_the_author_may_approve_a_t1_action(self, backend):
        """The rule is for T2. For T1 the SOP was already approved by a second person."""
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await world.decide(incident, actor=AUTHOR)
        await world.work()
        assert (await world.record(incident))["outcome"] == "resolved"

    async def test_a_t2_action_from_an_sop_with_no_author_on_record_cannot_be_approved(
        self, backend
    ):
        world = await make_world(backend)
        incident = await world.run(T2_JOB)
        action_id = uuid.UUID((await world.action(incident))["id"])
        other = await seed_action(
            world.db,
            world.tenant,
            incident.number,
            T2_JOB,
            step_id="other-step",
            tier="T2",
            verdict="require_approval",
            status="pending",
            expires_at=utcnow() + timedelta(hours=1),
        )
        assert other != action_id
        row = await action_store.get(world.db, world.tenant, other)
        with pytest.raises(action_store.NotAllowed, match="not on record"):
            await action_store.decide(
                world.db, world.tenant, APPROVER, other, approve=True, seen_hash=row.action_hash
            )

    async def test_a_rejection_escalates_with_the_approvers_reason(self, backend):
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await world.decide(incident, approve=False, note_text="month end; leave it")
        await world.work()
        record = await world.record(incident)
        assert record["outcome"] == "escalated" and world.autosys.force_starts == []
        assert "rejected by" in record["escalation_reason"]
        assert "month end; leave it" in record["escalation_reason"]
        story = reconstruct(await world.events(incident))
        assert story["action"]["decision"] == "rejected" and "executed" not in story["action"]

    async def test_a_decision_is_made_once(self, backend):
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await world.decide(incident, approve=False)
        for approve in (True, False):
            with pytest.raises(action_store.WrongState, match="not waiting for approval"):
                await world.decide(incident, approve=approve)
        assert (await world.action(incident))["status"] == "rejected"

    async def test_an_approval_cannot_be_used_twice(self, backend):
        """One approval, one execution, even if the run were somehow queued again."""
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await world.decide(incident)
        await world.work()
        assert world.autosys.force_starts == [T1_JOB]
        action_id = uuid.UUID((await world.action(incident))["id"])
        row, why = await action_store.authorise_execution(world.db, world.tenant, action_id, False)
        assert row is None and "no longer authorised" in why

    async def test_a_row_cannot_be_marked_executing_without_an_authorisation(self, backend):
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        with pytest.raises(IntegrityError):
            await set_columns(world, incident, execution_started_at=utcnow())
        with pytest.raises(IntegrityError):
            await set_columns(world, incident, status="approved")  # no approver, no hash
        action_id = uuid.UUID((await world.action(incident))["id"])
        row, _ = await action_store.authorise_execution(world.db, world.tenant, action_id, False)
        assert row is None, "a pending action is not authorised"

    async def test_nothing_above_t1_can_be_recorded_as_allowed(self, backend):
        """Decision D5, in the database."""
        world = await make_world(backend)
        incident = await world.run()
        with pytest.raises(IntegrityError):
            async with world.db.tenant(world.tenant) as conn:
                source = (
                    (
                        await conn.execute(
                            sa.select(actions).where(actions.c.incident_number == incident.number)
                        )
                    )
                    .one()
                    ._asdict()
                )
                source.update(id=uuid.uuid4(), step_id="another", tier="T2", verdict="allow")
                await conn.execute(sa.insert(actions).values(**source))

    async def test_what_was_decided_cannot_be_rewritten(self, pg):
        world = await make_world(pg, autonomy="approval")
        incident = await world.run()
        for values, message in (
            (dict(parameters={"job_name": "ANOTHER_JOB"}), "cannot be changed after the decision"),
            (dict(action_hash="0" * 64), "cannot be changed after the decision"),
            (dict(capability_id="autosys.job.delete"), "cannot be changed after the decision"),
            (dict(tier="T2"), "cannot be changed after the decision"),
            (dict(sop_author_id="someone-else"), "cannot be changed after the decision"),
            (dict(verdict="allow"), "can only be changed to a denial"),
        ):
            with pytest.raises(DBAPIError, match=message):
                await set_columns(world, incident, **values)
        await world.decide(incident)
        with pytest.raises(DBAPIError, match="approval decision cannot be changed"):
            await set_columns(world, incident, approver_id="user:someone-else")
        with pytest.raises(DBAPIError):
            async with world.db.tenant(world.tenant) as conn:
                await conn.execute(sa.delete(actions))
        assert (await world.action(incident))["parameters"] == {"job_name": T1_JOB}


# ── Approvals given in ServiceNow, on the change record ───────────────

SERVICENOW = {"approval": {"channels": ["servicenow"], "servicenow_group": "CAB"}}


class TestServiceNowApproval:
    async def test_an_approval_on_the_change_record_resumes_the_run(self, backend):
        world = await make_world(backend, autonomy="approval", rules=SERVICENOW)
        incident = await world.run()
        action = await world.action(incident)
        number = action["change_number"]
        assert world.snow.changes[number]["asked"] == "CAB" and action["channels"] == ["servicenow"]
        await world.work()
        assert (await world.action(incident))["status"] == "pending", "nobody has decided yet"

        world.snow.decide_change(number, "approved", "alice.cab", "alice@example.com", "ok")
        await world.work()  # the sweep collects the approval
        await world.work()  # the run resumes
        record, action = await world.record(incident), await world.action(incident)
        assert record["outcome"] == "resolved" and world.autosys.force_starts == [T1_JOB]
        assert action["approver"] == "user:servicenow|alice.cab"
        assert action["decided_via"] == "servicenow" and action["decision_note"] == "ok"
        story = reconstruct(await world.events(incident))
        assert story["action"]["decided_by"] == "user:servicenow|alice.cab"
        assert story["action"]["asked"] == ["servicenow"]

    async def test_a_rejection_on_the_change_record_escalates(self, backend):
        world = await make_world(backend, autonomy="approval", rules=SERVICENOW)
        incident = await world.run()
        number = (await world.action(incident))["change_number"]
        world.snow.decide_change(number, "rejected", "bob.cab", "bob@example.com", "not tonight")
        await world.work()
        await world.work()
        record = await world.record(incident)
        assert record["outcome"] == "escalated" and world.autosys.force_starts == []
        assert "not tonight" in record["escalation_reason"]

    async def test_an_approval_on_a_change_that_no_longer_carries_the_hash_is_not_accepted(
        self, backend
    ):
        world = await make_world(backend, autonomy="approval", rules=SERVICENOW)
        incident = await world.run()
        number = (await world.action(incident))["change_number"]
        world.snow.changes[number]["correlation"] = "f" * 64  # the change was edited
        world.snow.decide_change(number, "approved", "alice.cab", "alice@example.com")
        await world.work()
        await world.work()
        record, action = await world.record(incident), await world.action(incident)
        assert record["outcome"] == "escalated" and action["status"] == "invalid"
        assert world.autosys.force_starts == []
        assert "approval.not_accepted" in reconstruct(await world.events(incident))["types"]

    @pytest.mark.parametrize(
        "author_email, approver_email, accepted",
        [
            ("author@example.com", "alice@example.com", True),
            ("author@example.com", "AUTHOR@example.com", False),  # the same person
            ("author@example.com", "", False),  # ServiceNow gave no email
            ("", "alice@example.com", False),  # the author's email is not known
        ],
    )
    async def test_for_t2_the_servicenow_approver_must_be_shown_to_be_someone_else(
        self, backend, author_email, approver_email, accepted
    ):
        world = await make_world(backend, rules=SERVICENOW)
        issuer, subject = AUTHOR.id.split("|")
        async with world.db.tenant(world.tenant) as conn:
            await conn.execute(
                sa.insert(users).values(
                    id=uuid.uuid4(),
                    tenant_id=world.tenant,
                    issuer=issuer,
                    subject=subject,
                    email=author_email,
                    created_at=utcnow(),
                )
            )
        incident = await world.run(T2_JOB)
        number = (await world.action(incident))["change_number"]
        world.snow.decide_change(number, "approved", "someone", approver_email)
        await world.work()
        await world.work()
        record = await world.record(incident)
        assert record["outcome"] == ("resolved" if accepted else "escalated")
        assert world.autosys.force_starts == ([T2_JOB] if accepted else [])

    async def test_both_channels_can_be_open_and_the_first_decision_stands(self, backend):
        rules = {"approval": {"channels": ["ui", "servicenow"], "servicenow_group": "CAB"}}
        world = await make_world(backend, autonomy="approval", rules=rules)
        incident = await world.run()
        number = (await world.action(incident))["change_number"]
        await world.decide(incident, approve=False, note_text="rejected in the product")
        world.snow.decide_change(number, "approved", "alice.cab", "alice@example.com")
        await world.work()
        await world.work()
        assert (await world.record(incident))["outcome"] == "escalated"
        assert world.autosys.force_starts == []

    async def test_servicenow_is_polled_no_more_often_than_configured(self, backend):
        world = await make_world(
            backend,
            autonomy="approval",
            rules=SERVICENOW,
            settings=ActionSettings(servicenow_poll_seconds=3600),
        )
        incident = await world.run()
        number = (await world.action(incident))["change_number"]
        await world.work()  # the first sweep polls
        world.snow.decide_change(number, "approved", "alice.cab", "alice@example.com")
        await world.work()  # too soon to poll again
        assert (await world.action(incident))["status"] == "pending"


# ── Exit criterion 4: the kill switch ─────────────────────────────────


@pytest.fixture(params=["tenant", "global", "local"])
def switch(request, tmp_path):
    """Turn one of the three switches on and off for a world."""
    kind = request.param
    flag = tmp_path / "stop-writes"

    class Switch:
        name = kind
        settings = ActionSettings(
            servicenow_poll_seconds=0, kill_switch_file=str(flag) if kind == "local" else ""
        )

        async def on(self, world) -> None:
            if kind == "tenant":
                await action_store.set_kill_switch(world.db, world.tenant, ADMIN, True, "test")
            elif kind == "global":
                await action_store.set_global_kill_switch(world.backend._owner, ADMIN, True, "test")
            else:
                flag.write_text("stop", encoding="utf-8")

        async def off(self, world) -> None:
            if kind == "tenant":
                await action_store.set_kill_switch(world.db, world.tenant, ADMIN, False)
            elif kind == "global":
                await action_store.set_global_kill_switch(world.backend._owner, ADMIN, False)
            elif flag.exists():
                flag.unlink()

    return Switch()


class TestKillSwitch:
    async def test_an_in_flight_run_stops_before_execute(self, backend, switch):
        world = await make_world(backend, autonomy="approval", settings=switch.settings)
        incident = await world.run()
        await world.decide(incident)  # approved and queued: the next thing would be the write
        try:
            await switch.on(world)
            await world.work()
            record, action = await world.record(incident), await world.action(incident)
            assert world.autosys.force_starts == []
            assert record["outcome"] == "aborted" and action["status"] == "aborted"
            assert f"kill switch is on ({switch.name})" in record["escalation_reason"]
            types = reconstruct(await world.events(incident))["types"]
            assert "action.aborted" in types and "action.executing" not in types
        finally:
            await switch.off(world)

    async def test_a_queued_write_is_refused_at_the_connector(self, backend, switch):
        """The grant is already minted. The switch goes on. The connector still refuses."""
        from src.l1_agent.actions.service import ActionCall
        from src.l1_agent.connectors.sdk.base import StepContext, WriteNotAuthorized
        from src.l1_agent.models.sop import SOPStep

        world = await make_world(backend, autonomy="approval", settings=switch.settings)
        incident = await world.run()
        await world.decide(incident)
        action = await world.action(incident)
        run_id = uuid.UUID(action["run_id"])
        connectors = await world.service._processor._executor._hub.for_tenant(world.tenant)
        connectors.authorise_writes(world.actions)
        call = ActionCall(
            step=SOPStep(step_id="step-restart", capability=FORCE_START),
            incident=incident,
            sop=world.sop,
            tenant_id=world.tenant,
            run_id=run_id,
            connectors=connectors,
            read=None,
            note=None,
        )
        row = await action_store.get(world.db, world.tenant, uuid.UUID(action["id"]))
        executing, grant, why = await world.actions._authorise(call, row)
        assert executing is not None and grant is not None and why == ""
        context = StepContext(world.tenant, str(run_id), "step-restart")
        try:
            await switch.on(world)
            with pytest.raises(WriteNotAuthorized, match=f"kill switch is on: {switch.name}"):
                await connectors.invoke(FORCE_START, {"job_name": T1_JOB}, context, grant)
            assert world.autosys.force_starts == []
        finally:
            await switch.off(world)
        # With the switch off again the same grant is good: the switch was the only obstacle.
        result = await connectors.invoke(FORCE_START, {"job_name": T1_JOB}, context, grant)
        assert result.success and world.autosys.force_starts == [T1_JOB]

    async def test_no_grant_is_minted_while_the_switch_is_on(self, backend, switch):
        world = await make_world(backend, autonomy="approval", settings=switch.settings)
        incident = await world.run()
        await world.decide(incident)
        action_id = uuid.UUID((await world.action(incident))["id"])
        try:
            await switch.on(world)
            row, why = await action_store.authorise_execution(
                world.db, world.tenant, action_id, world.actions._local_switch()
            )
            assert row is None and f"kill switch is on: {switch.name}" in why
            assert (await world.action(incident))["status"] == "approved", "nothing was changed"
        finally:
            await switch.off(world)

    async def test_a_new_run_is_refused_at_the_decision_and_ends_aborted(self, backend, switch):
        world = await make_world(backend, settings=switch.settings)
        try:
            await switch.on(world)
            incident = await world.run()
            record, action = await world.record(incident), await world.action(incident)
            assert world.autosys.force_starts == [] and world.snow.changes == {}
            assert record["outcome"] == "aborted"
            assert action["verdict"] == "deny" and action["reason_code"] == "kill_switch"
        finally:
            await switch.off(world)
        after = await world.run()
        assert (await world.record(after))["outcome"] == "resolved", "released: writes resume"

    async def test_one_tenants_switch_does_not_stop_another_tenant(self, backend):
        stopped = await make_world(backend)
        running = await make_world(backend)
        await action_store.set_kill_switch(stopped.db, stopped.tenant, ADMIN, True, "incident 42")
        assert (await stopped.record(await stopped.run()))["outcome"] == "aborted"
        assert (await running.record(await running.run()))["outcome"] == "resolved"

    async def test_the_global_switch_stops_every_tenant_and_is_audited_in_each(self, backend):
        one, two = await make_world(backend), await make_world(backend)
        owner = backend._owner
        try:
            count = await action_store.set_global_kill_switch(owner, ADMIN, True, "bad release")
            assert count >= 2
            for world in (one, two):
                assert (await world.record(await world.run()))["outcome"] == "aborted"
                assert await world.actions.kill_switches(world.tenant) == ("global",)
            state = await action_store.global_kill_switch(one.db)
            assert state["on"] is True and state["reason"] == "bad release"
        finally:
            await action_store.set_global_kill_switch(owner, ADMIN, False, "fixed")
        for world in (one, two):
            from src.l1_agent.audit.log import events_for

            events = await events_for(world.db, world.tenant, limit=1000)
            switches = [
                (e["event_type"], e["payload"]["scope"])
                for e in events
                if e["event_type"].startswith("killswitch.")
            ]
            assert switches == [("killswitch.engaged", "global"), ("killswitch.released", "global")]

    async def test_engaging_and_releasing_are_audited_with_who_and_why(self, backend):
        world = await make_world(backend)
        operator = Actor("user", "https://idp.test|operator-1")
        await action_store.set_kill_switch(world.db, world.tenant, operator, True, "runaway job")
        policy = await action_store.get_policy(world.db, world.tenant)
        assert policy.kill_switch and policy.kill_reason == "runaway job"
        assert policy.kill_set_by == f"user:{operator.id}"
        await action_store.set_kill_switch(world.db, world.tenant, ADMIN, False, "resolved")
        from src.l1_agent.audit.log import events_for

        events = [
            e
            for e in await events_for(world.db, world.tenant, limit=1000)
            if e["event_type"].startswith("killswitch.")
        ]
        assert [(e["event_type"], e["actor"]["id"], e["payload"]["reason"]) for e in events] == [
            ("killswitch.engaged", operator.id, "runaway job"),
            ("killswitch.released", ADMIN.id, "resolved"),
        ]


# ── Exit criterion 5 for Autosys, and the other ways a write does not happen ──


class TestOutcomes:
    async def test_a_job_that_fails_again_is_not_verified_and_the_run_escalates(self, backend):
        world = await make_world(backend)
        incident = await world.run(FAILS_AGAIN)
        record, action = await world.record(incident), await world.action(incident)
        assert world.autosys.force_starts == [FAILS_AGAIN]
        assert record["outcome"] == "escalated" and action["status"] == "failed"
        assert "was not verified: it failed again" in record["escalation_reason"]
        assert "a started job cannot be un-started" in record["escalation_reason"]
        assert world.snow.changes[action["change_number"]]["closed"]["successful"] is False
        story = reconstruct(await world.events(incident))
        assert story["action"]["result"] == "failed"
        assert story["action"]["change_closed_successful"] is False
        assert story["steps"][-1] == ("step-restart", "escalated"), "no later step ran"

    async def test_a_job_that_never_finishes_within_the_wait_is_not_verified(self, backend):
        world = await make_world(backend)

        async def still_running(job_name: str):
            from src.l1_agent.adapters.base import AdapterResult

            return AdapterResult(
                success=True, data={"jobs": [{"job_name": job_name, "status": "RU"}]}
            )

        original = world.autosys.execute

        async def execute(parameters):
            if parameters.get("job_name") in world.autosys._reads_since_start:
                return await still_running(parameters["job_name"])
            return await original(parameters)

        world.autosys.execute = execute
        incident = await world.run()
        record = await world.record(incident)
        assert record["outcome"] == "escalated" and world.autosys.force_starts == [T1_JOB]
        assert "was not true after 6 read(s)" in record["escalation_reason"]
        assert world.sleeps == [2] * 5, "the SOP's five retries, two seconds apart"

    async def test_the_pre_check_stops_a_restart_of_a_job_that_is_not_failed(self, backend):
        """Between the SOP's own check and the action, the job started running again."""
        world = await make_world(backend)
        reads = {"n": 0}
        original = world.autosys.execute

        async def execute(parameters):
            from src.l1_agent.adapters.base import AdapterResult

            reads["n"] += 1
            if reads["n"] == 2:  # the action's pre-check
                job = parameters["job_name"]
                return AdapterResult(
                    success=True, data={"jobs": [{"job_name": job, "status": "RU"}]}
                )
            return await original(parameters)

        world.autosys.execute = execute
        incident = await world.run()
        record, action = await world.record(incident), await world.action(incident)
        assert world.autosys.force_starts == [] and action["status"] == "not_done"
        assert record["outcome"] == "escalated"
        assert record["step_results"][-1]["step_id"] == "step-not-restarted"
        assert "action.executing" not in reconstruct(await world.events(incident))["types"]

    async def test_no_change_record_no_write(self, backend):
        world = await make_world(backend)
        world.snow.change_templates = set()  # the template the SOP names does not exist
        incident = await world.run()
        record, action = await world.record(incident), await world.action(incident)
        assert world.autosys.force_starts == []
        assert record["outcome"] == "escalated" and action["status"] == "denied"
        assert action["verdict"] == "deny" and action["reason_code"] == "change_record_unavailable"
        assert "No change record could be opened" in record["escalation_reason"]
        verdicts = [
            e["payload"]["verdict"]
            for e in await world.events(incident)
            if e["event_type"] == "policy.decided"
        ]
        assert verdicts == ["allow", "deny"], "the denial is recorded as a second decision"

    async def test_an_itsm_without_change_management_cannot_back_a_write(self, backend):
        world = await make_world(backend)
        world.snow.open_change = None
        world.actions._itsm = type("NoChanges", (), {})()
        incident = await world.run()
        assert world.autosys.force_starts == []
        assert (await world.action(incident))["reason_code"] == "change_record_unavailable"

    async def test_a_job_off_the_connections_allow_list_is_denied(self, backend):
        world = await make_world(
            backend, connector_settings={"autosys": {"force_start_jobs": ["WEEKLY_*"]}}
        )
        incident = await world.run()
        record, action = await world.record(incident), await world.action(incident)
        assert world.autosys.force_starts == [] and world.snow.changes == {}
        assert record["outcome"] == "escalated"
        assert action["verdict"] == "deny" and action["reason_code"] == "not_on_allow_list"

    async def test_an_empty_allow_list_denies_everything(self, backend):
        world = await make_world(backend, connector_settings={"autosys": {"force_start_jobs": []}})
        incident = await world.run()
        assert world.autosys.force_starts == []
        assert (await world.action(incident))["reason_code"] == "not_on_allow_list"

    async def test_the_rate_limit_counts_executions(self, backend):
        world = await make_world(backend, rules={"rate": {"max_actions": 1, "per_minutes": 60}})
        first = await world.run()
        second = await world.run("NIGHTLY_OTHER_LOAD")
        assert (await world.record(first))["outcome"] == "resolved"
        assert (await world.action(second))["reason_code"] == "rate_limit"
        assert world.autosys.force_starts == [T1_JOB]

    async def test_a_change_freeze_denies(self, backend):
        now = utcnow()
        window = {
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "reason": "year end",
        }
        world = await make_world(backend, rules={"freeze": [window]})
        incident = await world.run()
        assert world.autosys.force_starts == []
        assert (await world.action(incident))["reason_code"] == "change_freeze"

    async def test_the_deployment_ceiling_overrides_the_tenant(self, backend):
        capped = ActionSettings(max_autonomy="approval", servicenow_poll_seconds=0)
        world = await make_world(backend, settings=capped)
        incident = await world.run()
        assert await world.record(incident) is None, "automatic for the tenant; approval here"
        assert (await world.action(incident))["verdict"] == "require_approval"
        none = await make_world(backend, settings=ActionSettings(max_tier="T0"))
        refused = await none.run()
        assert (await none.action(refused))["reason_code"] == "above_deployment_ceiling"
        assert none.autosys.force_starts == []

    async def test_a_newly_published_sop_version_starts_in_shadow(self, backend):
        from src.l1_agent.sop import store as sop_store
        from tests.actions_support import restart_content

        world = await make_world(backend)
        content = {**restart_content(), "title": "Nightly Settlement Job Failure - Restart (v2)"}
        version_id = await sop_store.create_draft(world.db, world.tenant, AUTHOR, content)
        digest = await sop_store.submit(world.db, world.tenant, AUTHOR, version_id)
        await sop_store.approve(world.db, world.tenant, REVIEWER, version_id, digest)
        await sop_store.publish(world.db, world.tenant, REVIEWER, version_id)
        incident = await world.run()
        action = await world.action(incident)
        assert action["sop_version"] == "2" and action["verdict"] == "shadow"
        assert world.autosys.force_starts == []

    async def test_an_action_is_not_repeated_for_the_same_incident(self, backend):
        world = await make_world(backend)
        incident = await world.run()
        assert world.autosys.force_starts == [T1_JOB]
        done = await action_store.get(
            world.db, world.tenant, uuid.UUID((await world.action(incident))["id"])
        )
        again = await seed_action(
            world.db,
            world.tenant,
            incident.number,
            T1_JOB,
            step_id="again",
            verdict="allow",
            status="allowed",
            call_digest=done.call_digest,
            valid_until=utcnow() + timedelta(minutes=5),
        )
        row, why = await action_store.authorise_execution(world.db, world.tenant, again, False)
        assert row is None and "already been carried out for this incident" in why

    async def test_a_run_interrupted_mid_write_is_not_repeated(self, backend):
        """The worker died after the write was authorised and before its result was recorded."""
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await world.decide(incident)
        action_id = uuid.UUID((await world.action(incident))["id"])
        row, _ = await action_store.authorise_execution(world.db, world.tenant, action_id, False)
        assert row.status == "executing"

        await world.work()  # another worker picks the run up
        record, action = await world.record(incident), await world.action(incident)
        assert world.autosys.force_starts == [], "not sent again"
        assert record["outcome"] == "needs_human" and action["status"] == "failed"
        assert "Whether it happened is not known" in record["escalation_reason"]
        assert "action.rolling_back" not in reconstruct(await world.events(incident))["types"]

    async def test_after_an_interruption_the_engine_looks_before_it_gives_up(self, backend):
        """Abuse case 8: killed between sendevent and the result record. Verify first; no second send."""
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await world.decide(incident)
        action_id = uuid.UUID((await world.action(incident))["id"])
        await action_store.authorise_execution(world.db, world.tenant, action_id, False)
        await world.autosys.force_start(T1_JOB)  # the event reached the scheduler; then the crash

        await world.work()
        record, action = await world.record(incident), await world.action(incident)
        assert world.autosys.force_starts == [T1_JOB], "exactly the one send"
        assert record["outcome"] == "resolved" and action["status"] == "verified"

    async def test_a_run_interrupted_while_verifying_verifies_again(self, backend):
        world = await make_world(backend, autonomy="approval")
        incident = await world.run()
        await world.decide(incident)
        action_id = uuid.UUID((await world.action(incident))["id"])
        await action_store.authorise_execution(world.db, world.tenant, action_id, False)
        await world.autosys.force_start(T1_JOB)  # the write went out and was recorded...
        await action_store.transition(
            world.db,
            world.tenant,
            action_id,
            from_statuses=("executing",),
            to_status="executed",
            event="action.executed",
        )
        await world.work()  # ...and the worker died before verifying
        assert world.autosys.force_starts == [T1_JOB], "the write was not repeated"
        assert (await world.record(incident))["outcome"] == "resolved"

    async def test_without_the_pipeline_an_action_step_is_refused(self, backend):
        """An engine built with no action service cannot write, whatever the policy says."""
        from tests.conftest import make_service

        world = await make_world(backend)
        world.service = make_service(
            world.db, adapters=world.adapters, snow=world.snow, sops_from_store=True
        )
        incident = await world.run()
        record = await world.record(incident)
        assert record["outcome"] == "escalated" and world.autosys.force_starts == []
        assert "Actions are not enabled here" in record["escalation_reason"]
        assert await world.action(incident) is None


class TestPolicyStore:
    async def test_a_tenant_with_no_policy_row_is_in_shadow(self, backend):
        tenant = await backend.new_tenant()
        policy = await action_store.get_policy(backend.db, tenant)
        assert policy.autonomy.value == "shadow" and not policy.kill_switch
        assert policy.to_dict()["rules"] == {}

    async def test_invalid_rules_change_nothing_and_say_why(self, backend):
        world = await make_world(backend)
        before = (await action_store.get_policy(world.db, world.tenant)).to_dict()
        with pytest.raises(action_store.ActionError) as err:
            await action_store.set_policy(
                world.db, world.tenant, ADMIN, autonomy="shadow", rules={"surprise": 1}
            )
        assert err.value.problems == ["unknown rule 'surprise'"]
        with pytest.raises(action_store.ActionError):
            await action_store.set_policy(world.db, world.tenant, ADMIN, autonomy="full-auto")
        assert (await action_store.get_policy(world.db, world.tenant)).to_dict() == before

    async def test_a_policy_change_is_audited(self, backend):
        from src.l1_agent.audit.log import events_for

        world = await make_world(backend, autonomy="shadow")
        await action_store.set_policy(world.db, world.tenant, ADMIN, autonomy="approval")
        changes = [
            e
            for e in await events_for(world.db, world.tenant, limit=1000)
            if e["event_type"] == "policy.changed"
        ]
        assert changes[-1]["actor"]["id"] == ADMIN.id
        assert changes[-1]["payload"]["autonomy_before"] == "shadow"
        assert changes[-1]["payload"]["autonomy"] == "approval"
        assert changes[-1]["payload"]["rules_changed"] is False

    async def test_stored_rules_that_no_longer_parse_mean_shadow_not_no_rules(self, backend):
        from src.l1_agent.db.models import policies

        world = await make_world(backend)
        async with world.db.tenant(world.tenant) as conn:
            await conn.execute(
                sa.update(policies)
                .where(policies.c.tenant_id == world.tenant)
                .values(rules={"from_a_later_version": True})
            )
        incident = await world.run()
        assert (await world.action(incident))["verdict"] == "shadow"
        assert world.autosys.force_starts == []
