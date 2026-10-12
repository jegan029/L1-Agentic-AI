"""The action pipeline: decide, change record, approval, pre-checks, execute, verify, roll back.

This is the only code that mints a grant, and it does so only after the
database has recorded that the action may execute (ADR 0008). The engine
calls `perform` for an ACTION step; the worker calls `sweep` to expire
approval requests and to collect approvals given in ServiceNow.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from src.l1_agent.actions import store
from src.l1_agent.actions.grants import GrantAuthority
from src.l1_agent.actions.policy import (
    CHANNEL_SERVICENOW,
    KILL_SWITCH,
    ActionRequest,
    Autonomy,
    Ruling,
    Situation,
    Verdict,
    evaluate,
)
from src.l1_agent.adapters.base import AdapterResult
from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.config.settings import ActionSettings
from src.l1_agent.connectors.itsm.base import ChangeRef, ChangeUnavailable
from src.l1_agent.connectors.sdk.base import OutcomeUnknown, StepContext, WriteNotAuthorized
from src.l1_agent.connectors.sdk.grant import Grant, call_digest
from src.l1_agent.connectors.sdk.manifest import Capability, Kind, RiskTier
from src.l1_agent.connectors.sdk.registry import ConnectorSet
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.engine import expressions
from src.l1_agent.engine.journal import RunSuspended
from src.l1_agent.engine.state_machine import RunEvent
from src.l1_agent.models.evidence import StepResult, StepStatus
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, SOPStep
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("actions")

E = RunEvent
CHANGE_UNAVAILABLE = "change_record_unavailable"
TOOL = "action"

# (status, AdapterResult or None, tool name, error text): what the engine's read call returns.
Read = Callable[[str, Dict[str, Any]], Awaitable[tuple]]


@dataclass
class ActionCall:
    """One ACTION step of one run, with what the pipeline needs to carry it out."""

    step: SOPStep  # parameters already rendered
    incident: Incident
    sop: SOP
    tenant_id: Optional[uuid.UUID]
    run_id: Optional[uuid.UUID]
    connectors: ConnectorSet
    read: Read
    note: Callable[[str], Awaitable[None]]


def action_hash(
    tenant_id: Any, incident: Incident, sop: SOP, capability_id: str, parameters
) -> str:
    """What an approval is for. Any change to any part gives a different hash."""
    return audit.digest(
        {
            "tenant": str(tenant_id),
            "incident": incident.sys_id,
            "sop": sop.sop_id,
            "sop_sha256": sop.content_hash,
            "capability": capability_id,
            "parameters": dict(parameters),
        }
    )


def _events_before_acting(row: Any) -> List[RunEvent]:
    if row.verdict == Verdict.ALLOW.value:
        return [E.POLICY_ALLOWED]
    return [E.APPROVAL_REQUIRED, E.APPROVAL_GRANTED]


class ActionService:
    def __init__(
        self,
        db: Database,
        itsm: Any,
        settings: Optional[ActionSettings] = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        grant_key: Optional[bytes] = None,
    ) -> None:
        self._db = db
        self._itsm = itsm
        self._settings = settings or ActionSettings()
        self._sleep = sleep
        # With a runner the key is this deployment's own, and the runner checks grants
        # with its public half (ADR 0010). Without one it is random and stays here.
        self._authority = GrantAuthority(private_key=grant_key)
        self._polled: Dict[uuid.UUID, float] = {}

    @property
    def grant_public_key(self) -> bytes:
        """What a runner checks this pipeline's grants with. Not a secret."""
        return self._authority.public_key

    # ── Kill switches ─────────────────────────────────────────────────

    def _local_switch(self) -> bool:
        path = self._settings.kill_switch_file
        return bool(path) and Path(path).exists()

    async def kill_switches(self, tenant_id: uuid.UUID) -> Tuple[str, ...]:
        """Every switch that is on now: global, tenant, local."""
        on = list(await store.switches_on(self._db, tenant_id))
        if self._local_switch():
            on.append(store.SWITCH_LOCAL)
        return tuple(on)

    # ── Grant verifier: what a connector asks before a write ──────────

    async def redeem(
        self,
        grant: Any,
        *,
        tenant_id: Any,
        run_id: str,
        step_id: str,
        capability_id: str,
        digest: str,
    ) -> None:
        if not isinstance(tenant_id, uuid.UUID):
            raise WriteNotAuthorized("A write is made for a tenant")
        on = await self.kill_switches(tenant_id)
        if on:
            metrics.increment("actions.refused_by_kill_switch")
            raise WriteNotAuthorized("kill switch is on: " + ", ".join(on))
        self._authority.redeem(
            grant,
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=step_id,
            capability_id=capability_id,
            digest=digest,
        )

    # ── The step ──────────────────────────────────────────────────────

    async def perform(self, call: ActionCall) -> StepResult:
        """Carry an ACTION step as far as it may go. Raises RunSuspended to wait for approval.

        The result's `data["events"]` is the path through the run state machine.
        """
        step = call.step
        capability = call.connectors.registry.capability(step.capability)
        if capability is None or capability.kind is not Kind.WRITE:
            return self._result(
                step,
                StepStatus.ERROR,
                [],
                f"'{step.capability}' is not a declared write capability",
            )
        if call.tenant_id is None or call.run_id is None:
            return self._result(
                step,
                StepStatus.ESCALATED,
                [E.POLICY_DENIED],
                "An action needs a recorded run; this run has none",
            )
        wanted = action_hash(
            call.tenant_id, call.incident, call.sop, capability.id, step.parameters
        )
        row = await store.find(self._db, call.tenant_id, call.run_id, step.step_id)
        if row is None:
            row = await self._decide(call, capability, wanted)
        elif row.action_hash != wanted and row.status not in store.FINAL:
            # The step no longer renders to what was decided on, or approved.
            row = await self._finish(
                call,
                row,
                ("pending", "approved", "allowed"),
                "invalid",
                "action.invalidated",
                "The action is no longer the one that was decided on: its parameters changed",
            )
        return await self._continue(call, capability, row)

    async def _continue(self, call: ActionCall, capability: Capability, row: Any) -> StepResult:
        if row.status in ("pending", "approved", "allowed"):
            row = await self._ensure_change(call, row)
        if row.status == "pending":
            if row.requested_at is None:
                row = await self._request_approval(call, capability, row)
            if utcnow() >= row.expires_at:
                row = await self._expire(call.tenant_id, row) or await self._reload(call, row)
            if row.status == "pending":
                raise RunSuspended(
                    f"waiting for approval of {capability.id} on {call.incident.number}"
                )
        if row.status in ("approved", "allowed"):
            return await self._act(call, capability, row)
        if row.status == "executed":
            # The write was recorded and the worker died while verifying. Reads can be repeated.
            return await self._verify_and_finish(call, capability, row, "")
        if row.status == "executing":
            # The worker died between sending the write and recording its result. The write
            # is not sent again. Reading is safe, so look first: if the target shows the
            # result the action was for, the run can go on.
            return await self._verify_and_finish(call, capability, row, "", interrupted=True)
        if row.status == "rolling_back":
            row = await self._finish(
                call,
                row,
                ("rolling_back",),
                "failed",
                "action.failed",
                "The run was interrupted while a rollback was being made. Whether it "
                "happened is not known, so it was not repeated",
                unknown=True,
            )
        return self._final(call.step, row)

    # ── Stage 1: decide ───────────────────────────────────────────────

    async def _decide(self, call: ActionCall, capability: Capability, wanted: str) -> Any:
        step, sop, tenant_id = call.step, call.sop, call.tenant_id
        policy = await store.get_policy(self._db, tenant_id)
        async with self._db.tenant(tenant_id) as conn:
            recent = await store.recent_executions(conn, tenant_id, policy.rules.rate_minutes)
            author = await store.sop_author(conn, tenant_id, sop.sop_id, sop.content_hash)
        now = utcnow()
        ruling = evaluate(
            ActionRequest(
                sop_id=sop.sop_id,
                sop_hash=sop.content_hash,
                capability_id=capability.id,
                is_declared_write=True,
                base_tier=capability.tier,
                targets=tuple(
                    str(step.parameters.get(n, "")) for n in capability.target_parameters
                ),
                now=now,
            ),
            Situation(
                kill_switches=await self.kill_switches(tenant_id),
                tenant_autonomy=policy.autonomy,
                rules=policy.rules,
                max_tier=RiskTier(self._settings.max_tier),
                max_autonomy=Autonomy(self._settings.max_autonomy),
                recent_actions=recent,
                refusals=tuple(call.connectors.refusals(capability.id, step.parameters)),
            ),
        )
        status = store.VERDICT_STATUS[ruling.verdict]
        tier = (ruling.tier or capability.tier or RiskTier.T2).value
        values: Dict[str, Any] = dict(
            run_id=call.run_id,
            incident_number=call.incident.number,
            step_id=step.step_id,
            sop_key=sop.sop_id,
            sop_version=str(sop.version),
            sop_hash=sop.content_hash,
            sop_author_id=author,
            capability_id=capability.id,
            parameters=dict(step.parameters),
            call_digest=call_digest(capability.id, step.parameters),
            action_hash=wanted,
            tier=tier,
            verdict=ruling.verdict.value,
            reason_code=ruling.reason_code,
            rule=ruling.rule[:300],
            status=status,
            change_template=sop.change_template,
            channels=list(policy.rules.channels) if status == "pending" else [],
        )
        if status == "pending":
            values["expires_at"] = now + timedelta(minutes=policy.rules.approval_ttl_minutes)
        elif status == "allowed":
            values["valid_until"] = now + timedelta(minutes=policy.rules.execute_within_minutes)
        elif status in store.FINAL:
            values["completed_at"] = now
        row = await store.record_decision(
            self._db,
            tenant_id,
            values,
            {
                "step_id": step.step_id,
                "sop_id": sop.sop_id,
                "sop_version": str(sop.version),
                "sop_sha256": sop.content_hash,
                "capability": capability.id,
                "parameters": dict(step.parameters),
                "action_sha256": wanted,
                "tier": tier,
                "verdict": ruling.verdict.value,
                "reason_code": ruling.reason_code,
                "rule": ruling.rule,
                "autonomy": ruling.autonomy.value,
            },
        )
        metrics.increment("actions.decisions", labels={"verdict": ruling.verdict.value})
        await self._announce(call, capability, ruling)
        return row

    async def _announce(self, call: ActionCall, capability: Capability, ruling: Ruling) -> None:
        what = f"{capability.id} {audit.canonical(call.step.parameters)}"
        if ruling.verdict is Verdict.SHADOW:
            await call.note(f"[L1 Agent] Shadow mode: would have run {what}. Nothing was changed.")
        elif ruling.verdict is Verdict.RECOMMEND:
            await call.note(
                f"[L1 Agent] Recommended action: {what}\n"
                f"SOP {call.sop.sop_id} v{call.sop.version}, step {call.step.step_id}. "
                "The agent has not done this. Handing over to L2."
            )
        elif ruling.verdict is Verdict.DENY:
            await call.note(f"[L1 Agent] Action not taken: {what}\nPolicy: {ruling.rule}")

    # ── Stage 2: the change record ────────────────────────────────────

    async def _ensure_change(self, call: ActionCall, row: Any) -> Any:
        """Open the change record the write is made under. Without one the decision is a denial."""
        if row.change_number:
            return row
        try:
            opener = getattr(self._itsm, "open_change", None)
            if opener is None:
                raise ChangeUnavailable("the ITSM connection has no change management")
            if not row.change_template:
                raise ChangeUnavailable("the SOP names no change template")
            ref = await opener(
                row.change_template,
                correlation=row.action_hash,
                short_description=(f"L1 Agent: {row.capability_id} for {row.incident_number}")[
                    :160
                ],
                description=(
                    f"Action: {row.capability_id}\n"
                    f"Parameters: {audit.canonical(row.parameters)}\n"
                    f"Incident: {row.incident_number}\n"
                    f"SOP: {row.sop_key} v{row.sop_version} ({row.sop_hash})\n"
                    f"Risk tier: {row.tier}\n"
                    f"Action hash: {row.action_hash}"
                ),
                incident_sys_id=call.incident.sys_id,
            )
        except Exception as exc:
            reason = " ".join(str(exc).split())[:200] or type(exc).__name__
            metrics.increment("actions.change_unavailable")
            return await self._finish(
                call,
                row,
                ("pending", "approved", "allowed"),
                "denied",
                "policy.decided",
                f"No change record could be opened ({reason}), so the action was not taken",
                values={"verdict": Verdict.DENY.value, "reason_code": CHANGE_UNAVAILABLE},
                payload={"verdict": Verdict.DENY.value, "reason_code": CHANGE_UNAVAILABLE},
            )
        return await store.note(
            self._db,
            call.tenant_id,
            row,
            "change.opened",
            {
                "change": ref.number,
                "template": row.change_template,
                "action_sha256": row.action_hash,
            },
            change_sys_id=ref.sys_id,
            change_number=ref.number,
        )

    # ── Stage 3: approval ─────────────────────────────────────────────

    async def _request_approval(self, call: ActionCall, capability: Capability, row: Any) -> Any:
        asked = 0
        channels = list(row.channels or [])
        if CHANNEL_SERVICENOW in channels:
            policy = await store.get_policy(self._db, call.tenant_id)
            try:
                asked = await self._itsm.request_change_approval(
                    ChangeRef(row.change_sys_id, row.change_number), policy.rules.servicenow_group
                )
            except Exception as exc:
                # The request can still be approved in the product, if that channel is on.
                logger.error("Could not ask for approval in ServiceNow: %s", type(exc).__name__)
        row = await store.note(
            self._db,
            call.tenant_id,
            row,
            "approval.requested",
            {
                "capability": row.capability_id,
                "parameters": row.parameters,
                "action_sha256": row.action_hash,
                "tier": row.tier,
                "change": row.change_number,
                "channels": channels,
                "asked_in_servicenow": asked,
                "expires_at": row.expires_at.isoformat(),
            },
            requested_at=utcnow(),
        )
        await call.note(
            f"[L1 Agent] Waiting for approval: {capability.id} "
            f"{audit.canonical(row.parameters)}\n"
            f"Change {row.change_number}, tier {row.tier}. "
            f"The request expires at {row.expires_at.isoformat(timespec='minutes')}."
        )
        metrics.increment("actions.approvals_requested")
        return row

    async def _expire(self, tenant_id: uuid.UUID, row: Any, wake: bool = False) -> Any:
        return await store.transition(
            self._db,
            tenant_id,
            row.id,
            from_statuses=("pending",),
            to_status="expired",
            event="approval.expired",
            payload={"expired_at": row.expires_at.isoformat()},
            values={"result": {"message": "The approval request expired with no decision"}},
            wake=wake,
        )

    # ── Stages 4 to 9 ─────────────────────────────────────────────────

    async def _act(self, call: ActionCall, capability: Capability, row: Any) -> StepResult:
        step = call.step
        waiting = ("approved", "allowed")
        on = await self.kill_switches(call.tenant_id)
        if on:
            row = await self._finish(
                call,
                row,
                waiting,
                "aborted",
                "action.aborted",
                "Stopped before the write: kill switch is on (" + ", ".join(on) + ")",
            )
            return self._final(step, row)
        if row.valid_until is None or utcnow() >= row.valid_until:
            row = await self._finish(
                call,
                row,
                waiting,
                "invalid",
                "action.invalidated",
                "The decision was not acted on in time and no longer applies",
            )
            return self._final(step, row)

        # Pre-checks: the reads the capability declares.
        for check in capability.pre_checks:
            params = self._read_parameters(call, check.capability, check.parameters)
            status, result, _tool, error = await call.read(check.capability, params)
            passed = (
                status == StepStatus.SUCCESS
                and result is not None
                and self._holds(check.assertion, result, step)
            )
            if not passed:
                why = error or f"{check.assertion} is not true"
                row = await self._finish(
                    call,
                    row,
                    waiting,
                    "not_done",
                    "action.not_done",
                    f"Pre-check {check.capability} did not pass ({why}); nothing was changed",
                    evidence=result.evidence_snippet if result else "",
                )
                return self._final(step, row)

        context = StepContext(call.tenant_id, str(call.run_id), step.step_id)
        if capability.supports_dry_run:
            try:
                trial = await call.connectors.invoke(
                    capability.id, step.parameters, context, dry_run=True
                )
            except Exception as exc:
                trial = AdapterResult(success=False, error=f"{type(exc).__name__}: {exc}")
            if not trial.success:
                row = await self._finish(
                    call,
                    row,
                    waiting,
                    "not_done",
                    "action.not_done",
                    f"The dry run was refused ({trial.error}); nothing was changed",
                )
                return self._final(step, row)

        executing, grant, why = await self._authorise(call, row)
        if executing is None:
            if "kill switch" in why:
                to, event = "aborted", "action.aborted"
            elif "already" in why:
                to, event = "not_done", "action.not_done"
            else:
                to, event = "invalid", "action.invalidated"
            row = await self._finish(call, row, waiting, to, event, f"Not executed: {why}")
            return self._final(step, row)
        row = executing

        try:
            written = await call.connectors.invoke(capability.id, step.parameters, context, grant)
        except OutcomeUnknown:
            # A runner took the write and did not answer. It is not sent again. As after
            # a crash at this point, the target is read to learn whether it was made.
            metrics.increment("actions.outcome_unknown")
            return await self._verify_and_finish(
                call,
                capability,
                row,
                "",
                interrupted=True,
                lost="The runner took the write and returned no result",
            )
        except Exception as exc:
            written = AdapterResult(success=False, error=f"{type(exc).__name__}: {exc}")
        if not written.success:
            metrics.increment("actions.failed")
            return await self._roll_back(
                call, capability, row, f"The write failed: {written.error}", ""
            )
        metrics.increment("actions.executed", labels={"capability": capability.id})
        row = await store.transition(
            self._db,
            call.tenant_id,
            row.id,
            from_statuses=("executing",),
            to_status="executed",
            event="action.executed",
            payload={"output_sha256": audit.digest(written.raw_output or written.data)},
            values={"result": {"evidence": written.evidence_snippet[:2000]}},
        )
        await self._note_change(
            row, f"L1 Agent ran {capability.id} {audit.canonical(step.parameters)}."
        )
        return await self._verify_and_finish(call, capability, row, written.evidence_snippet)

    async def _authorise(self, call: ActionCall, row: Any) -> Tuple[Any, Optional[Grant], str]:
        """Record that the write may be made, and only then mint the grant for it."""
        executing, why = await store.authorise_execution(
            self._db, call.tenant_id, row.id, self._local_switch()
        )
        if executing is None:
            return None, None, why
        grant = self._authority.mint(
            decision_id=str(executing.id),
            tenant_id=str(call.tenant_id),
            run_id=str(call.run_id),
            step_id=executing.step_id,
            capability_id=executing.capability_id,
            call_digest=executing.call_digest,
        )
        return executing, grant, ""

    async def _authorise_rollback(
        self, call: ActionCall, row: Any, capability_id: str, parameters: Mapping[str, Any]
    ) -> Optional[Grant]:
        """A grant for the declared rollback of a write that was made under a decision."""
        if await self.kill_switches(call.tenant_id):
            return None
        moved = await store.transition(
            self._db,
            call.tenant_id,
            row.id,
            from_statuses=("executing", "executed"),
            to_status="rolling_back",
            event="action.rolling_back",
            payload={"rollback": capability_id, "parameters": dict(parameters)},
        )
        if moved is None:
            return None
        return self._authority.mint(
            decision_id=str(row.id),
            tenant_id=str(call.tenant_id),
            run_id=str(call.run_id),
            step_id=row.step_id,
            capability_id=capability_id,
            call_digest=call_digest(capability_id, parameters),
        )

    async def _verify_and_finish(
        self,
        call: ActionCall,
        capability: Capability,
        row: Any,
        evidence: str,
        interrupted: bool = False,
        lost: str = "The run was interrupted while the write was being made",
    ) -> StepResult:
        step, spec = call.step, capability.verify
        assertion = step.verify.get("assert") or spec.assertion
        retries = int(step.verify.get("retries", spec.retries))
        wait = float(step.verify.get("wait_seconds", spec.wait_seconds))
        params = self._read_parameters(call, spec.capability, spec.parameters)
        why, last = "the verification read did not succeed", None
        for attempt in range(retries + 1):
            if attempt and wait:
                await self._sleep(wait)
            status, result, _tool, error = await call.read(spec.capability, params)
            if status != StepStatus.SUCCESS or result is None:
                why = f"the verification read failed: {error}"
                continue
            last = result
            if self._holds(assertion, result, step):
                row = await self._finish(
                    call,
                    row,
                    ("executed", "executing"),
                    "verified",
                    "action.verified",
                    f"Verified: {assertion}",
                    evidence="\n".join(filter(None, [evidence, result.evidence_snippet])),
                    payload={"assertion": assertion, "attempts": attempt + 1},
                )
                await self._close_change(row, True, f"Verified: {assertion}")
                return self._final(step, row)
            if spec.failed and self._holds(spec.failed, result, step):
                why = f"it failed again ({spec.failed})"
                break
            why = f"{assertion} was not true after {attempt + 1} read(s)"
        if interrupted:
            # Nothing shows the write happened, and nothing shows it did not. Rolling back
            # something that may not have been done is a second unrequested change.
            row = await self._finish(
                call,
                row,
                ("executing",),
                "failed",
                "action.failed",
                f"{lost}, and the target does "
                f"not show its result ({why}). Whether it happened is not known, so it was "
                "not repeated",
                unknown=True,
            )
            return self._final(step, row)
        return await self._roll_back(
            call,
            capability,
            row,
            f"The action ran but was not verified: {why}",
            "\n".join(filter(None, [evidence, last.evidence_snippet if last else ""])),
        )

    async def _roll_back(
        self, call: ActionCall, capability: Capability, row: Any, why: str, evidence: str
    ) -> StepResult:
        """Undo the write where the manifest says how, then hand the incident to a person."""
        open_statuses = ("executing", "executed", "rolling_back")
        declared = capability.rollback
        if not declared or declared.startswith("none:"):
            reason = declared.partition(":")[2].strip() or "none is declared"
            row = await self._finish(
                call,
                row,
                open_statuses,
                "failed",
                "action.failed",
                f"{why}. No rollback: {reason}",
                evidence=evidence,
                payload={"rollback": "none", "reason": why},
            )
        else:
            params = self._read_parameters(call, declared, {})
            grant = await self._authorise_rollback(call, row, declared, params)
            undone = AdapterResult(success=False, error="the rollback was not authorised")
            if grant is not None:
                context = StepContext(call.tenant_id, str(call.run_id), call.step.step_id)
                try:
                    undone = await call.connectors.invoke(declared, params, context, grant)
                except Exception as exc:
                    undone = AdapterResult(success=False, error=f"{type(exc).__name__}: {exc}")
            if undone.success:
                row = await self._finish(
                    call,
                    row,
                    open_statuses,
                    "rolled_back",
                    "action.rolled_back",
                    f"{why}. Rolled back with {declared}",
                    evidence=evidence,
                    payload={"rollback": declared, "reason": why},
                )
            else:
                row = await self._finish(
                    call,
                    row,
                    open_statuses,
                    "rollback_failed",
                    "action.rollback_failed",
                    f"{why}. The rollback ({declared}) also failed: {undone.error}",
                    evidence=evidence,
                    payload={"rollback": declared, "reason": why},
                )
        metrics.increment("actions.not_verified")
        await self._close_change(row, False, (row.result or {}).get("message", why))
        return self._final(call.step, row)

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _holds(assertion: str, result: AdapterResult, step: SOPStep) -> bool:
        """An assertion that cannot be evaluated does not hold."""
        try:
            return bool(
                expressions.evaluate(
                    assertion, {"result": result.data, "params": dict(step.parameters)}
                )
            )
        except expressions.ExpressionError:
            return False

    @staticmethod
    def _read_parameters(
        call: ActionCall, capability_id: str, fixed: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Those of the action's parameters that the other capability declares, plus `fixed`."""
        other = call.connectors.registry.capability(capability_id)
        declared = (other.parameters.get("properties", {}) if other else {}).keys()
        shared = {k: v for k, v in call.step.parameters.items() if k in declared}
        return {**shared, **dict(fixed)}

    async def _reload(self, call: ActionCall, row: Any) -> Any:
        return await store.get(self._db, call.tenant_id, row.id)

    async def _finish(
        self,
        call: ActionCall,
        row: Any,
        from_statuses: Tuple[str, ...],
        to_status: str,
        event: str,
        message: str,
        *,
        evidence: str = "",
        unknown: bool = False,
        values: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Move the action to a status and store what the step result will say.

        If another attempt moved it first, that attempt's status stands.
        """
        stored = {"message": message, "evidence": evidence[:2000], "unknown": unknown}
        columns: Dict[str, Any] = {"result": stored, **(values or {})}
        if to_status == "denied":
            columns["rule"] = message[:300]
        moved = await store.transition(
            self._db,
            call.tenant_id,
            row.id,
            from_statuses=from_statuses,
            to_status=to_status,
            event=event,
            payload={"message": message, **(payload or {})},
            values=columns,
        )
        return moved if moved is not None else await self._reload(call, row)

    def _final(self, step: SOPStep, row: Any) -> StepResult:
        """The step result for an action that has finished, from its record alone."""
        stored = row.result or {}
        message = stored.get("message") or row.rule or row.status
        acted = _events_before_acting(row)
        asked = [E.APPROVAL_REQUIRED, E.APPROVAL_REFUSED]
        status_events: Dict[str, Tuple[StepStatus, List[RunEvent]]] = {
            "shadowed": (StepStatus.SKIP, [E.POLICY_SHADOW]),
            "recommended": (StepStatus.ESCALATED, [E.POLICY_RECOMMEND]),
            "rejected": (StepStatus.ESCALATED, asked),
            "expired": (StepStatus.ESCALATED, asked),
            "not_done": (StepStatus.FAIL, acted + [E.ACTION_NOT_DONE]),
            "verified": (StepStatus.SUCCESS, acted + [E.ACTION_VERIFIED]),
            "rolled_back": (StepStatus.ESCALATED, acted + [E.ACTION_FAILED, E.ROLLBACK_DONE]),
            "rollback_failed": (StepStatus.ESCALATED, acted + [E.ACTION_FAILED, E.ROLLBACK_DONE]),
        }
        if row.status == "denied":
            killed = row.reason_code == KILL_SWITCH
            status, events = StepStatus.ESCALATED, [E.ABORT if killed else E.POLICY_DENIED]
        elif row.status == "aborted":
            status, events = StepStatus.ESCALATED, acted + [E.ABORT]
        elif row.status == "invalid":
            stale_allow = row.verdict == Verdict.ALLOW.value
            status, events = StepStatus.ESCALATED, [E.POLICY_DENIED] if stale_allow else asked
        elif row.status == "failed" and stored.get("unknown"):
            status, events = StepStatus.ERROR, acted + [E.ERROR]
        elif row.status == "failed":
            status, events = StepStatus.ESCALATED, acted + [E.ACTION_FAILED, E.ROLLBACK_DONE]
        else:
            status, events = status_events[row.status]
        if row.status == "shadowed":
            message = (
                f"Shadow mode: would have run {row.capability_id} "
                f"{audit.canonical(row.parameters)}. Nothing was changed"
            )
        elif row.status == "recommended":
            message = (
                f"Recommended, not done: {row.capability_id} {audit.canonical(row.parameters)}"
            )
        elif row.status == "rejected":
            message = f"The action was rejected by {row.approver_id}" + (
                f": {row.decision_note}" if row.decision_note else ""
            )
        return self._result(
            step,
            status,
            events,
            message,
            evidence=stored.get("evidence", ""),
            action={
                "id": str(row.id),
                "status": row.status,
                "verdict": row.verdict,
                "reason_code": row.reason_code,
                "tier": row.tier,
                "change": row.change_number,
                "approver": row.approver_id or "",
                "action_sha256": row.action_hash,
            },
        )

    @staticmethod
    def _result(
        step: SOPStep,
        status: StepStatus,
        events: List[RunEvent],
        message: str,
        *,
        evidence: str = "",
        action: Optional[Dict[str, Any]] = None,
    ) -> StepResult:
        succeeded = status in (StepStatus.SUCCESS, StepStatus.SKIP)
        return StepResult(
            step_id=step.step_id,
            step_type=step.step_type,
            status=status,
            evidence=evidence,
            error_message="" if succeeded else message,
            tool_called=TOOL,
            input_summary=f"{step.capability} {audit.canonical(step.parameters)}"[:300],
            output_summary=message[:300],
            data={"events": [e.value for e in events], "action": action or {}},
        )

    # ── The change record, best effort ────────────────────────────────

    async def _note_change(self, row: Any, text: str) -> None:
        if not row.change_sys_id:
            return
        try:
            await self._itsm.note_change(ChangeRef(row.change_sys_id, row.change_number), text)
        except Exception as exc:
            logger.warning("Could not add a note to %s: %s", row.change_number, type(exc).__name__)

    async def _close_change(self, row: Any, successful: bool, notes: str) -> None:
        """Close the change with the result. A refusal is recorded and changes nothing else."""
        if not row.change_sys_id:
            return
        closed, error = True, ""
        try:
            await self._itsm.close_change(
                ChangeRef(row.change_sys_id, row.change_number), successful, notes[:1000]
            )
        except Exception as exc:
            closed, error = False, " ".join(str(exc).split())[:200] or type(exc).__name__
            logger.warning("Could not close %s: %s", row.change_number, error)
        await store.note(
            self._db,
            row.tenant_id,
            row,
            "change.closed" if closed else "change.close_failed",
            {"change": row.change_number, "successful": successful, "error": error},
        )

    # ── The worker's part: expiry, and approvals given in ServiceNow ──

    async def sweep(self, tenant_id: uuid.UUID) -> int:
        """Expire approval requests and collect ServiceNow approvals. Returns how many moved."""
        waiting = await store.pending(self._db, tenant_id)
        if not waiting:
            return 0
        now, moved = utcnow(), 0
        poll_due = (
            time.monotonic() - self._polled.get(tenant_id, 0.0)
            >= self._settings.servicenow_poll_seconds
        )
        polled = False
        for row in waiting:
            if row.expires_at is not None and now >= row.expires_at:
                moved += bool(await self._expire(tenant_id, row, wake=True))
            elif CHANNEL_SERVICENOW in (row.channels or []) and row.change_sys_id and poll_due:
                polled = True
                try:
                    moved += await self._collect(tenant_id, row)
                except Exception as exc:
                    logger.warning(
                        "Could not read approvals for %s: %s", row.change_number, type(exc).__name__
                    )
        if polled:
            self._polled[tenant_id] = time.monotonic()
        return moved

    async def _collect(self, tenant_id: uuid.UUID, row: Any) -> int:
        """Apply an approval or rejection made on the change record in ServiceNow."""
        found = await self._itsm.change_approval(ChangeRef(row.change_sys_id, row.change_number))
        if found.state not in ("approved", "rejected"):
            return 0
        problem = ""
        if found.correlation != row.action_hash:
            problem = "The change record no longer carries the hash of this action"
        elif not found.approver:
            problem = "ServiceNow did not say who decided"
        elif found.state == "approved" and row.tier == RiskTier.T2.value:
            async with self._db.tenant(tenant_id) as conn:
                author = await store.author_email(conn, tenant_id, row.sop_author_id)
            approver = found.approver_email.strip().lower()
            if not author or not approver or author == approver:
                problem = (
                    "The ServiceNow approver could not be shown to be someone other than "
                    "the author of the SOP, which a T2 action requires"
                )
        if problem:
            await store.transition(
                self._db,
                tenant_id,
                row.id,
                from_statuses=("pending",),
                to_status="invalid",
                event="approval.not_accepted",
                payload={"via": CHANNEL_SERVICENOW, "reason": problem},
                values={"result": {"message": problem}},
                wake=True,
            )
            return 1
        try:
            await store.decide(
                self._db,
                tenant_id,
                Actor("user", f"servicenow|{found.approver}"),
                row.id,
                approve=found.state == "approved",
                seen_hash=found.correlation,
                note_text=found.comment,
                via=CHANNEL_SERVICENOW,
                approver_email=found.approver_email,
            )
        except store.ActionError as exc:
            logger.info("ServiceNow decision on %s not applied: %s", row.change_number, exc)
        return 1
