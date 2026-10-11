"""SOP execution engine: drives a published SOP through the run state machine.

Outcomes fail closed (docs/adr/0002-fail-closed-outcomes.md). The run has no
outcome until it reaches a terminal state; RESOLVED is reached only through a
RESOLVE step entered straight after a VERIFY step whose assertion passed;
anything the engine cannot handle ends as NEEDS_HUMAN.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.adapters.schemas import STEP_ADAPTERS, validate_step_parameters
from src.l1_agent.connectors.catalog import (
    CONNECTOR_ADAPTERS,
    STEP_CAPABILITIES,
    connectors_for_adapters,
    default_registry,
)
from src.l1_agent.connectors.sdk.base import Refused, StepContext
from src.l1_agent.connectors.sdk.registry import ConnectorSet
from src.l1_agent.engine import expressions
from src.l1_agent.engine.journal import RunSuspended
from src.l1_agent.engine.parameters import ParameterError, bind_parameters, render
from src.l1_agent.engine.state_machine import (
    RunEvent,
    RunState,
    is_terminal,
    outcome_of,
    transition,
)
from src.l1_agent.models.evidence import (
    ExecutionOutcome,
    ExecutionSummary,
    StepResult,
    StepStatus,
    UnverifiedResolutionError,
)
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import CONNECTOR_STEP_TYPES, SOP, SOPStep, StepType
from src.l1_agent.sop.validator import LEGACY_ANY_FAILED_RULE, validate_sop
from src.l1_agent.utils.logging import get_logger, set_correlation_id
from src.l1_agent.utils.metrics import metrics
from src.l1_agent.utils.retry import CircuitBreaker, retry_with_backoff

if TYPE_CHECKING:
    from src.l1_agent.engine.journal import EventPublisher, RunJournal

logger = get_logger("executor")


@dataclass
class _Run:
    """What a step needs to call a connector: the tenant's connectors and who is asking."""

    connectors: ConnectorSet
    tenant_id: Any = None
    run_id: str = ""
    # For an ACTION step: the SOP version, the run's own id, and where notes go.
    sop: Optional[SOP] = None
    journal_run_id: Any = None
    note: Any = None


class _AdapterFailure(Exception):
    """Raised inside the retry closure when an adapter returns success=False."""

    def __init__(self, result: AdapterResult) -> None:
        self.result = result
        super().__init__(result.error or "adapter returned failure")


class _FixedConnectors:
    """The same connectors for every tenant: demo mode and tests."""

    def __init__(self, connectors: ConnectorSet) -> None:
        self._connectors = connectors

    async def for_tenant(self, tenant_id: Any) -> ConnectorSet:
        return self._connectors


class SOPExecutor:
    """Executes SOP steps, calling a connector capability for each check.

    Each step produces a StepResult with evidence. The executor tracks the
    entire run and produces an ExecutionSummary at the end.

    `adapters` wraps ready adapters (the mocks) as connectors. `connectors`
    is the service's hub, which builds each tenant's connectors from its
    connections.
    """

    def __init__(
        self,
        adapters: Optional[Dict[str, BaseAdapter]] = None,
        circuit_breakers: Optional[Dict[str, CircuitBreaker]] = None,
        retry_max_attempts: int = 3,
        retry_base_delay: float = 1.0,
        event_bus: Optional["EventPublisher"] = None,
        connectors: Any = None,
        breaker_factory: Optional[Any] = None,
        actions: Any = None,
    ) -> None:
        # The action pipeline (ADR 0008). Without one, an ACTION step is refused.
        self._actions = actions
        self._hub = connectors or _FixedConnectors(connectors_for_adapters(adapters or {}))
        self._circuit_breakers = circuit_breakers or {}
        self._breaker_factory = breaker_factory
        self._tenant_breakers: Dict[tuple, CircuitBreaker] = {}
        self._retry_max = retry_max_attempts
        self._retry_delay = retry_base_delay
        self._event_bus = event_bus

    async def execute_sop(
        self,
        incident: Incident,
        sop: SOP,
        work_note_callback: Any = None,
        journal: Optional["RunJournal"] = None,
    ) -> ExecutionSummary:
        """Execute an SOP for a given incident.

        Args:
            incident: The incident being processed.
            sop: The matched SOP to execute.
            work_note_callback: Async callable(sys_id, note) for posting work notes.
            journal: Durable record of the run. A step it already holds a result
                for is not executed again; its recorded result is used instead.

        Returns:
            ExecutionSummary with all step results and a terminal outcome.
        """
        set_correlation_id(incident.correlation_id)
        start_time = time.perf_counter()
        summary = ExecutionSummary(
            incident_number=incident.number,
            sop_id=sop.sop_id,
            sop_title=sop.title,
        )
        state = RunState.BINDING
        reason = ""
        verified_by = ""

        logger.info(
            "Starting SOP execution: %s (%s) for incident %s",
            sop.title,
            sop.sop_id,
            incident.number,
        )

        try:
            problems = validate_sop(sop)
            if problems:
                state = transition(state, RunEvent.SOP_INVALID)
                reason = "SOP failed validation: " + "; ".join(problems[:5])
            else:
                try:
                    values = bind_parameters(sop, incident)
                    steps = [self._render_step(s, values) for s in sop.steps]
                    state = transition(state, RunEvent.PARAMS_BOUND)
                except ParameterError as exc:
                    state = transition(state, RunEvent.PARAMS_INVALID)
                    reason = f"Parameter binding failed: {exc}"

            if not is_terminal(state):
                if not (journal and journal.resumed):
                    await self._note(
                        work_note_callback,
                        incident,
                        f"[L1 Agent] Starting SOP: {sop.title} ({sop.sop_id})\n"
                        f"Steps available: {len(sop.steps)}",
                        "start",
                    )
                tenant_id = getattr(journal, "tenant_id", None)
                run = _Run(
                    connectors=await self._hub.for_tenant(tenant_id),
                    tenant_id=tenant_id,
                    run_id=str(getattr(journal, "run_id", "") or incident.number),
                    sop=sop,
                    journal_run_id=getattr(journal, "run_id", None),
                    note=work_note_callback,
                )
                if self._actions is not None:
                    # Writes by these connectors are checked against this pipeline's grants.
                    run.connectors.authorise_writes(self._actions)
                state, reason, verified_by = await self._run_steps(
                    incident, steps, values, summary, work_note_callback, journal, run
                )
        except RunSuspended:
            raise  # waiting for a person; the run has no outcome yet
        except Exception as exc:  # engine fault: never leave a run without an outcome
            logger.error("SOP execution aborted by an internal error: %s", exc)
            if not is_terminal(state):
                state = transition(state, RunEvent.ERROR)
            reason = f"Internal error: {type(exc).__name__}"
            verified_by = ""

        elapsed = (time.perf_counter() - start_time) * 1000
        summary.total_duration_ms = elapsed
        summary.completed_at = datetime.now(timezone.utc).isoformat()
        try:
            summary.conclude(outcome_of(state), reason=reason, verified_by=verified_by)
        except UnverifiedResolutionError:
            # Last line of defence: the summary itself refuses an unverified RESOLVED.
            logger.error("Run reached RESOLVED without a verification record; needs a human")
            summary.conclude(
                ExecutionOutcome.NEEDS_HUMAN,
                reason="Internal error: resolution had no verification record",
            )

        metrics.increment("sop.executions", labels={"outcome": summary.outcome_label})
        metrics.observe("sop.execution_duration_ms", elapsed)
        logger.info(
            "SOP execution complete: %s -> %s (%.0fms)",
            sop.sop_id,
            summary.outcome_label,
            elapsed,
        )
        return summary

    # ── Run loop ──────────────────────────────────────────────────────

    async def _run_steps(
        self,
        incident: Incident,
        steps: List[SOPStep],
        values: Dict[str, Any],
        summary: ExecutionSummary,
        work_note_callback: Any,
        journal: Optional["RunJournal"] = None,
        run: Optional["_Run"] = None,
    ) -> tuple:
        """Walk the step graph. Returns (terminal state, reason, verified_by).

        With a journal, a step that completed in an earlier attempt is replayed
        from its recorded result. The route through the graph depends only on
        step results, so replay reaches the same point without repeating a call.
        """
        state = RunState.EXECUTING
        reason = ""
        verified_by = ""
        index: Dict[str, SOPStep] = {s.step_id: s for s in steps}
        current_id = steps[0].step_id
        visited: set = set()

        while not is_terminal(state):
            step = index.get(current_id) if current_id else None
            if step is None or current_id in visited:
                # The validator rules both out; reaching here is an engine fault.
                state = transition(state, RunEvent.STEPS_EXHAUSTED)
                reason = "SOP ended without an ESCALATE or RESOLVE step"
                break
            visited.add(current_id)
            step_type = step.step_type.upper()

            if step_type == StepType.VERIFY.value and not step.requires_approval:
                state = transition(state, RunEvent.VERIFY_STARTED)

            recorded = journal.completed(step.step_id) if journal else None
            if recorded is not None:
                result = recorded
                summary.step_results.append(result)
            else:
                await self._publish(
                    {
                        "type": "step_executing",
                        "incident_number": incident.number,
                        "step_id": step.step_id,
                        "step_type": step.step_type,
                        "description": step.description,
                    }
                )
                if journal:
                    await journal.step_started(step)
                step_start = time.perf_counter()
                result = await self._execute_step(step, incident, summary, values, state, run)
                result.duration_ms = (time.perf_counter() - step_start) * 1000
                summary.step_results.append(result)
                if journal:
                    # Committed before anything else is told about the result.
                    await journal.step_completed(step, result)

                await self._publish(
                    {
                        "type": "step_done",
                        "incident_number": incident.number,
                        "step_id": step.step_id,
                        "status": result.status.value,
                        "output_summary": result.output_summary[:200],
                        "duration_ms": round(result.duration_ms, 1),
                    }
                )
                await self._note(
                    work_note_callback,
                    incident,
                    f"[L1 Agent] Step {step.step_id} ({step.step_type}): "
                    f"{result.status.value.upper()}\n{result.output_summary}",
                    f"step {step.step_id}",
                )

            # Decide the transition and the next step.
            next_id = ""
            path = result.data.get("events") if step_type == StepType.ACTION.value else None
            if path:
                # An action reports its route through the table; the table decides the state.
                state = transition(state, RunEvent.ACTION_STARTED)
                for name in path:
                    state = transition(state, RunEvent(name))
                verified_by = ""
                if is_terminal(state):
                    reason = result.error_message or result.output_summary
                elif state != RunState.EXECUTING:
                    state = transition(state, RunEvent.ERROR)
                    reason = f"Action {step.step_id} ended in an unexpected state"
                elif result.status == StepStatus.FAIL:
                    next_id = step.on_failure
                    if not next_id:
                        state = transition(state, RunEvent.ESCALATE)
                        reason = f"Step {step.step_id} was not done: {result.error_message}"
                else:
                    next_id = step.on_success
            elif result.status == StepStatus.ERROR:
                state = transition(state, RunEvent.ERROR)
                reason = f"Step {step.step_id} could not run: {result.error_message}"
            elif result.status == StepStatus.ESCALATED:
                state = transition(state, RunEvent.ESCALATE)
                reason = result.error_message or "Step required escalation"
            elif step_type == StepType.RESOLVE.value:
                # RESOLVED only if the table allows it from the current state,
                # i.e. straight after a passed VERIFY. Otherwise NEEDS_HUMAN.
                state = transition(state, RunEvent.RESOLVE)
                if state != RunState.RESOLVED:
                    reason = "RESOLVE step reached without a passed verification"
            elif step_type == StepType.VERIFY.value:
                if result.status == StepStatus.SUCCESS:
                    state = transition(state, RunEvent.VERIFY_PASSED)
                    verified_by = step.step_id
                    next_id = step.on_success
                else:
                    state = transition(state, RunEvent.VERIFY_FAILED)
                    verified_by = ""
                    next_id = step.on_failure
                    if not next_id:
                        state = transition(state, RunEvent.ESCALATE)
                        reason = f"Verification {step.step_id} did not pass: {result.error_message}"
            elif step_type == StepType.NOTE.value:
                state = transition(state, RunEvent.NOTE_DONE)
                next_id = step.on_success
            else:
                state = transition(state, RunEvent.STEP_DONE)
                verified_by = ""
                if result.status == StepStatus.FAIL:
                    next_id = step.on_failure
                    if not next_id:
                        # An unset failure edge is an escalation (ADR 0002).
                        state = transition(state, RunEvent.ESCALATE)
                        reason = f"Step {step.step_id} failed: {result.error_message}"
                else:
                    # A DECISION picks its own branch through recommended_next_step.
                    next_id = result.recommended_next_step or step.on_success

            if state not in (RunState.VERIFIED, RunState.RESOLVED):
                verified_by = ""
            current_id = next_id

        return state, reason, verified_by if state == RunState.RESOLVED else ""

    # ── Steps ─────────────────────────────────────────────────────────

    @staticmethod
    def _render_step(step: SOPStep, values: Dict[str, Any]) -> SOPStep:
        return SOPStep(
            step_id=step.step_id,
            step_type=step.step_type,
            description=step.description,
            parameters=render(step.parameters, values),
            capability=step.capability,
            verify=step.verify,
            expected_output=step.expected_output,
            on_success=step.on_success,
            on_failure=step.on_failure,
            requires_approval=step.requires_approval,
            timeout_seconds=step.timeout_seconds,
        )

    async def _execute_step(
        self,
        step: SOPStep,
        incident: Incident,
        summary: ExecutionSummary,
        values: Dict[str, Any],
        state: RunState,
        run: Optional["_Run"] = None,
    ) -> StepResult:
        """Execute a single SOP step and return the result."""
        logger.info("Executing step %s (%s)", step.step_id, step.step_type)

        # Approval gate: no approval workflow exists yet, so hand to a person.
        if step.requires_approval:
            return StepResult(
                step_id=step.step_id,
                step_type=step.step_type,
                status=StepStatus.ESCALATED,
                error_message="Step requires approval; escalating to L2",
                tool_called="approval_gate",
                input_summary=step.description,
                output_summary="Escalated: approval required",
            )

        try:
            step_type = step.step_type.upper()
            if step_type == StepType.NOTE.value:
                return self._handle_note_step(step)
            if step_type == StepType.DECISION.value:
                return self._handle_decision_step(step, incident, summary, values)
            if step_type == StepType.VERIFY.value:
                return await self._handle_verify_step(step, incident, summary, values, run)
            if step_type == StepType.ESCALATE.value:
                text = step.parameters.get("reason") or step.parameters.get("text")
                text = text or step.description or "SOP routed this incident to L2"
                return StepResult(
                    step_id=step.step_id,
                    step_type=step.step_type,
                    status=StepStatus.ESCALATED,
                    error_message=text,
                    evidence=text,
                    tool_called="escalate",
                    input_summary=step.description,
                    output_summary=text[:200],
                )
            if step_type == StepType.RESOLVE.value:
                text = step.parameters.get("text", step.description) or "Resolved"
                verified = state == RunState.VERIFIED
                return StepResult(
                    step_id=step.step_id,
                    step_type=step.step_type,
                    status=StepStatus.SUCCESS if verified else StepStatus.FAIL,
                    evidence=text if verified else "",
                    error_message="" if verified else "no passed verification",
                    tool_called="resolve",
                    input_summary=step.description,
                    output_summary=text[:200] if verified else "Refused: not verified",
                )
            if step_type in CONNECTOR_STEP_TYPES:
                return await self._handle_adapter_step(step, run)
            if step_type == StepType.ACTION.value:
                return await self._handle_action_step(step, incident, run)
            return self._error(step, f"Unknown step type: {step.step_type}", "none")
        except RunSuspended:
            raise
        except Exception as exc:
            logger.error("Step %s raised: %s", step.step_id, exc)
            return self._error(step, f"{type(exc).__name__}: {exc}", step.step_type)

    @staticmethod
    def _error(step: SOPStep, message: str, tool: str) -> StepResult:
        return StepResult(
            step_id=step.step_id,
            step_type=step.step_type,
            status=StepStatus.ERROR,
            error_message=message,
            tool_called=tool,
            input_summary=str(step.parameters),
            output_summary=f"Cannot continue: {message}"[:200],
        )

    def _breaker(self, run: "_Run", connector_id: str) -> Optional[CircuitBreaker]:
        """One breaker per connection: explicit ones first, else one per tenant and connector."""
        explicit = self._circuit_breakers.get(connector_id) or self._circuit_breakers.get(
            CONNECTOR_ADAPTERS.get(connector_id, "")
        )
        if explicit is not None or self._breaker_factory is None:
            return explicit
        key = (run.tenant_id, connector_id)
        if key not in self._tenant_breakers:
            self._tenant_breakers[key] = self._breaker_factory()
        return self._tenant_breakers[key]

    async def _call_adapter(
        self, name: str, parameters: Dict[str, Any], run: Optional["_Run"], step_id: str = ""
    ) -> tuple:
        """Validate parameters and call the capability with retry and breaker.

        `name` is a legacy step type or a capability id.
        Returns (StepStatus, AdapterResult or None, tool name, error text).
        ERROR means the engine could not make the call at all.
        """
        capability_id = STEP_CAPABILITIES.get(name, name)
        connector_id = default_registry().owner(capability_id)
        if not connector_id:
            return StepStatus.ERROR, None, "none", f"Unknown step type: {name}"
        legacy_key = STEP_ADAPTERS.get(name, connector_id)

        problems = validate_step_parameters(name, parameters)
        if problems:
            return StepStatus.ERROR, None, legacy_key, "; ".join(problems)
        if run is None or run.connectors.connector(connector_id) is None:
            return StepStatus.ERROR, None, legacy_key, f"Adapter '{legacy_key}' not configured"
        refused = run.connectors.refusals(capability_id, parameters)
        if refused:
            return StepStatus.ERROR, None, legacy_key, "; ".join(refused)

        tool = run.connectors.tool_name(capability_id)
        context = StepContext(run.tenant_id, run.run_id, step_id)

        async def _run() -> AdapterResult:
            result = await run.connectors.invoke(capability_id, parameters, context)
            if not result.success:
                # Raise so retry_with_backoff can see the failure and retry.
                raise _AdapterFailure(result)
            return result

        try:
            adapter_result = await retry_with_backoff(
                _run,
                max_attempts=self._retry_max,
                base_delay=self._retry_delay,
                circuit_breaker=self._breaker(run, connector_id),
            )
        except RuntimeError as exc:
            # Circuit breaker open: the target is unavailable.
            return StepStatus.ERROR, None, tool, str(exc)
        except _AdapterFailure as exc:
            return StepStatus.FAIL, exc.result, tool, exc.result.error
        except (Refused, LookupError, PermissionError) as exc:
            return StepStatus.ERROR, None, tool, str(exc)
        except Exception as exc:
            return StepStatus.FAIL, None, tool, f"All retries exhausted: {exc}"
        return StepStatus.SUCCESS, adapter_result, tool, ""

    async def _handle_adapter_step(self, step: SOPStep, run: Optional["_Run"]) -> StepResult:
        """Call the capability the step names: its own, or its legacy step type's."""
        name = step.capability or step.step_type.upper()
        status, adapter_result, tool, error = await self._call_adapter(
            name, step.parameters, run, step.step_id
        )
        if status == StepStatus.ERROR:
            return self._error(step, error, tool)
        return StepResult(
            step_id=step.step_id,
            step_type=step.step_type,
            status=status,
            evidence=adapter_result.evidence_snippet if adapter_result else "",
            error_message=error,
            tool_called=tool,
            input_summary=str(step.parameters),
            output_summary=adapter_result.summary() if adapter_result else error,
            data=dict(adapter_result.data)
            if adapter_result and status == StepStatus.SUCCESS
            else {},
        )

    async def _handle_action_step(
        self, step: SOPStep, incident: Incident, run: Optional["_Run"]
    ) -> StepResult:
        """Hand a write to the action pipeline. The engine itself never makes one."""
        if self._actions is None or run is None or run.sop is None:
            text = "Actions are not enabled here; the step was not carried out"
            return StepResult(
                step_id=step.step_id,
                step_type=step.step_type,
                status=StepStatus.ESCALATED,
                error_message=text,
                tool_called="action",
                input_summary=f"{step.capability} {step.parameters}",
                output_summary=text,
                data={"events": [RunEvent.POLICY_DENIED.value]},
            )
        from src.l1_agent.actions.service import ActionCall

        async def read(name: str, parameters: Dict[str, Any]) -> tuple:
            return await self._call_adapter(name, parameters, run, step.step_id)

        async def note(text: str) -> None:
            await self._note(run.note, incident, text, f"action {step.step_id}")

        return await self._actions.perform(
            ActionCall(
                step=step,
                incident=incident,
                sop=run.sop,
                tenant_id=run.tenant_id,
                run_id=run.journal_run_id,
                connectors=run.connectors,
                read=read,
                note=note,
            )
        )

    async def _handle_verify_step(
        self,
        step: SOPStep,
        incident: Incident,
        summary: ExecutionSummary,
        values: Dict[str, Any],
        run: Optional["_Run"] = None,
    ) -> StepResult:
        """Run the check and apply the assertion. Inconclusive counts as a fail."""
        check = str(step.parameters.get("check", ""))
        check = check.upper() if check.upper() in STEP_CAPABILITIES else check
        check_params = step.parameters.get("parameters", {})
        assertion = step.parameters.get("assert", "")
        retries = int(step.parameters.get("retries", 0))
        wait_seconds = float(step.parameters.get("wait_seconds", 0))

        last_error = ""
        last_result: Optional[AdapterResult] = None
        tool = check
        for attempt in range(retries + 1):
            if attempt and wait_seconds:
                await asyncio.sleep(wait_seconds)
            status, adapter_result, tool, error = await self._call_adapter(
                check, check_params, run, step.step_id
            )
            if status == StepStatus.ERROR:
                return self._error(step, error, tool)
            if status != StepStatus.SUCCESS or adapter_result is None:
                last_error = f"check failed: {error}"
                continue
            last_result = adapter_result
            context = self._context(incident, summary, values)
            context["result"] = adapter_result.data
            try:
                passed = expressions.evaluate(assertion, context)
            except expressions.ExpressionError as exc:
                last_error = f"assertion could not be evaluated: {exc}"
                continue
            if passed:
                return StepResult(
                    step_id=step.step_id,
                    step_type=step.step_type,
                    status=StepStatus.SUCCESS,
                    evidence=adapter_result.evidence_snippet,
                    tool_called=tool,
                    input_summary=f"assert {assertion}",
                    output_summary=f"Verified: {assertion}\n{adapter_result.summary()}",
                    data=dict(adapter_result.data),
                )
            last_error = f"assertion is false: {assertion}"

        return StepResult(
            step_id=step.step_id,
            step_type=step.step_type,
            status=StepStatus.FAIL,
            evidence=last_result.evidence_snippet if last_result else "",
            error_message=last_error,
            tool_called=tool,
            input_summary=f"assert {assertion}",
            output_summary=f"Not verified: {last_error}"
            + (f"\n{last_result.summary()}" if last_result else ""),
            data=dict(last_result.data) if last_result else {},
        )

    @staticmethod
    def _handle_note_step(step: SOPStep) -> StepResult:
        """Handle a NOTE step: just produce text for the work note."""
        text = step.parameters.get("text", step.description)
        return StepResult(
            step_id=step.step_id,
            step_type=step.step_type,
            status=StepStatus.SUCCESS,
            evidence=text,
            tool_called="note",
            input_summary=step.description,
            output_summary=text[:200],
        )

    @staticmethod
    def _context(
        incident: Incident, summary: ExecutionSummary, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Names available to DECISION rules and VERIFY assertions."""
        checks = [
            r
            for r in summary.step_results
            if r.step_type.upper() in CONNECTOR_STEP_TYPES
            or r.step_type.upper() == StepType.VERIFY.value
        ]
        return {
            "steps": {r.step_id: r.data for r in summary.step_results},
            "status": {r.step_id: r.status.value for r in summary.step_results},
            "incident": incident.to_dict(),
            "params": dict(values),
            "any_failed": any(r.status != StepStatus.SUCCESS for r in checks),
            "all_success": all(r.status == StepStatus.SUCCESS for r in checks),
        }

    def _handle_decision_step(
        self,
        step: SOPStep,
        incident: Incident,
        summary: ExecutionSummary,
        values: Dict[str, Any],
    ) -> StepResult:
        """Evaluate a rule to choose a branch: true takes on_success.

        The exact rule ``any_failed`` keeps its historical direction (true
        takes on_failure). A rule that cannot be parsed or evaluated is an
        ERROR, which ends the run as NEEDS_HUMAN; it never picks a branch.
        """
        rule = step.parameters.get("rule", "")
        context = self._context(incident, summary, values)
        try:
            if rule == LEGACY_ANY_FAILED_RULE:
                take_success = not context["any_failed"]
            else:
                take_success = expressions.evaluate(rule, context)
        except expressions.ExpressionError as exc:
            return self._error(step, f"decision rule could not be evaluated: {exc}", "decision")

        branch = step.on_success if take_success else step.on_failure
        result = StepResult(
            step_id=step.step_id,
            step_type=step.step_type,
            status=StepStatus.SUCCESS if (take_success or branch) else StepStatus.FAIL,
            tool_called="decision",
            input_summary=f"Rule: {rule}",
            output_summary=f"Decision: {'success branch' if take_success else 'failure branch'}",
            recommended_next_step=branch,
        )
        if not take_success and not branch:
            result.error_message = f"rule '{rule}' is false and no failure branch is set"
        return result

    # ── Side effects ──────────────────────────────────────────────────

    async def _publish(self, event: Dict[str, Any]) -> None:
        if self._event_bus:
            await self._event_bus.publish(event)

    @staticmethod
    async def _note(callback: Any, incident: Incident, note: str, where: str) -> None:
        if not callback:
            return
        try:
            await callback(incident.sys_id, note)
        except Exception as exc:
            logger.warning("Work note callback failed (%s): %s", where, exc)
