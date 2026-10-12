"""Verification failure triggers rollback where declared, then escalation (exit criterion 5).

Autosys declares no rollback, so this uses a connector written for the test: a
valve that can be opened (the write), closed (its declared rollback) and read.
The pipeline is the real one, called the way the engine calls it; the route it
reports is walked through the real transition table.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Tuple

from src.l1_agent.actions import store as action_store
from src.l1_agent.actions.service import ActionCall, ActionService
from src.l1_agent.adapters.base import AdapterResult
from src.l1_agent.audit.log import Actor, events_for
from src.l1_agent.config.settings import ActionSettings
from src.l1_agent.connectors.sdk.base import Connector, Health, HealthStatus, StepContext
from src.l1_agent.connectors.sdk.manifest import (
    AllowListBinding,
    Capability,
    Family,
    Kind,
    Manifest,
    PreCheck,
    RiskTier,
    Verify,
)
from src.l1_agent.connectors.sdk.registry import ConnectorSet, Registry
from src.l1_agent.connectors.sdk.schema import closed
from src.l1_agent.demo import DEMO_INCIDENTS, MockServiceNowClient
from src.l1_agent.engine.state_machine import RunEvent, RunState, transition
from src.l1_agent.models.evidence import StepResult, StepStatus
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, SOPStep
from tests.conftest import make_service

ADMIN = Actor("user", "https://idp.test|admin-1")
SOP_ID, SOP_HASH = "SOP-VALVE", "c" * 64
_NAME = closed({"name": {"type": "string", "maxLength": 20}}, required=["name"])
_ALLOW = (AllowListBinding("name", "valves", "exact"),)


class Valve(Connector):
    manifest = Manifest(
        id="valve",
        version="1.0.0",
        family=Family.OPERATIONS,
        display_name="Valve",
        config=closed({"valves": {"type": "array"}}),
        capabilities=(
            Capability("valve.status", Kind.READ, RiskTier.T0, "Read a valve", _NAME),
            Capability(
                id="valve.open",
                kind=Kind.WRITE,
                tier=RiskTier.T1,
                summary="Open a valve",
                parameters=_NAME,
                scopes=("operate",),
                allow_lists=_ALLOW,
                target_parameters=("name",),
                supports_dry_run=True,
                pre_checks=(PreCheck("valve.status", 'result.state == "closed"'),),
                verify=Verify("valve.status", 'result.state == "open" and result.flow > 0'),
                rollback="valve.close",
            ),
            Capability(
                id="valve.close",
                kind=Kind.WRITE,
                tier=RiskTier.T1,
                summary="Close a valve",
                parameters=_NAME,
                scopes=("operate",),
                allow_lists=_ALLOW,
                target_parameters=("name",),
                verify=Verify("valve.status", 'result.state == "closed"'),
                rollback="none: closed is the safe state",
            ),
        ),
    )

    def __init__(self, **behaviour: Any) -> None:
        super().__init__({"valves": ["V1"]})
        self.state = "closed"
        self.calls: List[Tuple[str, str]] = []
        self.flows = behaviour.get("flows", True)  # opening actually lets something through
        self.close_works = behaviour.get("close_works", True)
        self.dry_run_ok = behaviour.get("dry_run_ok", True)
        self.open_accepted = behaviour.get("open_accepted", True)

    async def _execute(self, capability, parameters) -> AdapterResult:
        flow = 5 if self.state == "open" and self.flows else 0
        return AdapterResult(success=True, data={"state": self.state, "flow": flow})

    async def _dry_run(self, capability, parameters) -> AdapterResult:
        self.calls.append(("dry_run", capability.id))
        return AdapterResult(success=self.dry_run_ok, error="" if self.dry_run_ok else "jammed")

    async def _execute_write(self, capability, parameters) -> AdapterResult:
        self.calls.append(("write", capability.id))
        if capability.id == "valve.open":
            if not self.open_accepted:
                return AdapterResult(success=False, error="actuator fault")
            self.state = "open"
            return AdapterResult(success=True, evidence_snippet="valve V1 opened")
        if not self.close_works:
            return AdapterResult(success=False, error="stuck open")
        self.state = "closed"
        return AdapterResult(success=True, evidence_snippet="valve V1 closed")

    async def health(self) -> Health:
        return Health(HealthStatus.HEALTHY)


class Rig:
    """One run with a valve SOP, and the call the engine would make for its ACTION step."""

    def __init__(self, backend, valve: Valve) -> None:
        self.backend, self.valve = backend, valve
        self.snow = MockServiceNowClient()
        self.notes: List[str] = []

    async def build(self, incident_index: int = 0) -> "Rig":
        db = self.backend.db
        self.tenant = await self.backend.new_tenant("valve")
        self.actions = ActionService(db, self.snow, ActionSettings(servicenow_poll_seconds=0))
        self.connectors = ConnectorSet(Registry([Valve]), {"valve": self.valve})
        self.connectors.authorise_writes(self.actions)
        await action_store.set_policy(
            db,
            self.tenant,
            ADMIN,
            autonomy="automatic",
            rules={
                "sop_autonomy": {SOP_ID: {"level": "automatic", "content_hash": SOP_HASH}},
                "automatic": [{"sop_id": SOP_ID, "capability": "valve.open", "targets": ["V*"]}],
            },
        )
        self.incident = Incident.from_servicenow(DEMO_INCIDENTS[incident_index])
        ingested = await make_service(db).ingest(self.tenant, self.incident, Actor.system("test"))
        self.run_id = ingested.run_id
        return self

    def call(self) -> ActionCall:
        sop = SOP(sop_id=SOP_ID, title="valve", version="1", change_template="Valve change")
        sop.content_hash = SOP_HASH

        async def read(name: str, parameters: Dict[str, Any]) -> tuple:
            context = StepContext(self.tenant, str(self.run_id), "open")
            result = await self.connectors.invoke(name, parameters, context)
            return StepStatus.SUCCESS, result, "Valve", ""

        async def note(text: str) -> None:
            self.notes.append(text)

        return ActionCall(
            step=SOPStep(
                step_id="open",
                step_type="ACTION",
                capability="valve.open",
                parameters={"name": "V1"},
                on_success="next",
            ),
            incident=self.incident,
            sop=sop,
            tenant_id=self.tenant,
            run_id=self.run_id,
            connectors=self.connectors,
            read=read,
            note=note,
        )

    async def perform(self) -> StepResult:
        return await self.actions.perform(self.call())

    async def row(self):
        return await action_store.find(self.backend.db, self.tenant, self.run_id, "open")

    async def event_types(self) -> List[str]:
        events = await events_for(
            self.backend.db, self.tenant, correlation_id=str(self.run_id), limit=500
        )
        return [e["event_type"] for e in events]


def walk(result: StepResult) -> List[RunState]:
    """The states an ACTION step's reported route passes through, from EXECUTING."""
    state = transition(RunState.EXECUTING, RunEvent.ACTION_STARTED)
    states = [state]
    for name in result.data["events"]:
        state = transition(state, RunEvent(name))
        states.append(state)
    return states


class TestRollback:
    async def test_a_verified_write_is_not_rolled_back(self, backend):
        rig = await Rig(backend, Valve()).build()
        result = await rig.perform()
        assert result.status == StepStatus.SUCCESS and rig.valve.state == "open"
        assert rig.valve.calls == [("dry_run", "valve.open"), ("write", "valve.open")]
        assert walk(result) == [RunState.POLICY_CHECK, RunState.ACTING, RunState.EXECUTING]
        assert (await rig.row()).status == "verified"

    async def test_failed_verification_rolls_back_then_escalates(self, backend):
        rig = await Rig(backend, Valve(flows=False)).build()
        result = await rig.perform()

        assert rig.valve.calls == [
            ("dry_run", "valve.open"),
            ("write", "valve.open"),
            ("write", "valve.close"),
        ]
        assert rig.valve.state == "closed", "the write was undone"
        assert result.status == StepStatus.ESCALATED
        assert "was not verified" in result.error_message
        assert "Rolled back with valve.close" in result.error_message
        assert walk(result) == [
            RunState.POLICY_CHECK,
            RunState.ACTING,
            RunState.ROLLING_BACK,
            RunState.ESCALATED,
        ]
        row = await rig.row()
        assert row.status == "rolled_back"
        assert rig.snow.changes[row.change_number]["closed"]["successful"] is False
        types = await rig.event_types()
        order = [
            types.index(t)
            for t in (
                "action.executing",
                "action.executed",
                "action.rolling_back",
                "action.rolled_back",
            )
        ]
        assert order == sorted(order)

    async def test_a_write_the_target_refused_is_rolled_back_too(self, backend):
        """The call failed, but the engine cannot know nothing changed, so it undoes it."""
        rig = await Rig(backend, Valve(open_accepted=False)).build()
        result = await rig.perform()
        assert result.status == StepStatus.ESCALATED
        assert "The write failed: actuator fault" in result.error_message
        assert rig.valve.calls[-1] == ("write", "valve.close")
        assert (await rig.row()).status == "rolled_back"

    async def test_a_rollback_that_fails_is_recorded_and_still_escalates(self, backend):
        rig = await Rig(backend, Valve(flows=False, close_works=False)).build()
        result = await rig.perform()
        assert rig.valve.state == "open", "still open: a person has to act"
        assert result.status == StepStatus.ESCALATED
        assert "The rollback (valve.close) also failed: stuck open" in result.error_message
        assert walk(result)[-1] == RunState.ESCALATED
        assert (await rig.row()).status == "rollback_failed"
        assert "action.rollback_failed" in await rig.event_types()

    async def test_the_rollback_is_a_write_and_needs_its_own_grant(self, backend):
        """With the kill switch on after the write, the rollback is not made either."""
        valve = Valve(flows=False)
        rig = await Rig(backend, valve).build()
        opened = valve._execute_write

        async def open_then_stop(capability, parameters):
            result = await opened(capability, parameters)
            await action_store.set_kill_switch(backend.db, rig.tenant, ADMIN, True, "stop")
            return result

        valve._execute_write = open_then_stop
        result = await rig.perform()
        assert valve.calls == [("dry_run", "valve.open"), ("write", "valve.open")]
        assert valve.state == "open" and result.status == StepStatus.ESCALATED
        assert "the rollback was not authorised" in result.error_message
        assert (await rig.row()).status == "rollback_failed"

    async def test_a_refused_dry_run_stops_before_the_write(self, backend):
        rig = await Rig(backend, Valve(dry_run_ok=False)).build()
        result = await rig.perform()
        assert rig.valve.calls == [("dry_run", "valve.open")] and rig.valve.state == "closed"
        assert (
            result.status == StepStatus.FAIL
            and "dry run was refused (jammed)" in result.error_message
        )
        assert walk(result) == [RunState.POLICY_CHECK, RunState.ACTING, RunState.EXECUTING]
        assert (await rig.row()).status == "not_done"
        assert "action.executing" not in await rig.event_types()

    async def test_a_failed_pre_check_stops_before_the_dry_run(self, backend):
        valve = Valve()
        valve.state = "open"  # already open: the pre-check wants it closed
        rig = await Rig(backend, valve).build()
        result = await rig.perform()
        assert valve.calls == [] and result.status == StepStatus.FAIL
        assert "Pre-check valve.status did not pass" in result.error_message

    async def test_performing_the_step_again_returns_the_recorded_result(self, backend):
        """A resumed run asks again. Nothing is done twice."""
        rig = await Rig(backend, Valve(flows=False)).build()
        first = await rig.perform()
        calls = list(rig.valve.calls)
        second = await rig.perform()
        assert rig.valve.calls == calls
        assert (second.status, second.data["events"]) == (first.status, first.data["events"])
        assert second.error_message == first.error_message

    async def test_a_capability_that_is_not_a_declared_write_is_an_error(self, backend):
        rig = await Rig(backend, Valve()).build()
        call = rig.call()
        for capability in ("valve.status", "valve.explode"):
            call.step.capability = capability
            result = await rig.actions.perform(call)
            assert result.status == StepStatus.ERROR and result.data["events"] == []
        assert rig.valve.calls == [] and await rig.row() is None

    async def test_an_action_without_a_recorded_run_is_denied(self, backend):
        rig = await Rig(backend, Valve()).build()
        call = rig.call()
        call.run_id = None
        result = await rig.actions.perform(call)
        assert result.status == StepStatus.ESCALATED and rig.valve.calls == []
        assert walk(result) == [RunState.POLICY_CHECK, RunState.ESCALATED]

    async def test_a_second_tenant_cannot_see_or_use_the_first_ones_action(self, backend):
        rig = await Rig(backend, Valve()).build()
        await rig.perform()
        other = await backend.new_tenant("other")
        row = await rig.row()
        assert await action_store.find(backend.db, other, rig.run_id, "open") is None
        assert await action_store.list_actions(backend.db, other) == []
        allowed, why = await action_store.authorise_execution(backend.db, other, row.id, False)
        assert allowed is None and uuid.UUID(str(row.id)) == row.id
