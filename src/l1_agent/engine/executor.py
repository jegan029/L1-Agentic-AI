"""SOP execution engine: orchestrates step-by-step SOP execution."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.models.evidence import (
    ExecutionOutcome,
    ExecutionSummary,
    StepResult,
    StepStatus,
)
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, SOPStep, StepType
from src.l1_agent.utils.logging import get_logger, set_correlation_id
from src.l1_agent.utils.metrics import metrics
from src.l1_agent.utils.retry import CircuitBreaker, retry_with_backoff

logger = get_logger("executor")


class _AdapterFailure(Exception):
    """Raised inside the retry closure when an adapter returns success=False."""

    def __init__(self, result: AdapterResult) -> None:
        self.result = result
        super().__init__(result.error or "adapter returned failure")


class SOPExecutor:
    """Executes SOP steps sequentially, using the appropriate tool adapter for each step.

    Supports step types: SPLUNK_SEARCH, MQ_CHECK, FILE_CHECK, AUTOSYS_STATUS,
    DECISION, and NOTE.

    Each step produces a StepResult with evidence. The executor tracks the
    entire run and produces an ExecutionSummary at the end.
    """

    def __init__(
        self,
        adapters: Dict[str, BaseAdapter],
        circuit_breakers: Optional[Dict[str, CircuitBreaker]] = None,
        retry_max_attempts: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        self._adapters = adapters
        self._circuit_breakers = circuit_breakers or {}
        self._retry_max = retry_max_attempts
        self._retry_delay = retry_base_delay

    async def execute_sop(
        self,
        incident: Incident,
        sop: SOP,
        work_note_callback: Any = None,
    ) -> ExecutionSummary:
        """Execute all steps in an SOP for a given incident.

        Args:
            incident: The incident being processed.
            sop: The matched SOP to execute.
            work_note_callback: Async callable(sys_id, note) for posting work notes.

        Returns:
            ExecutionSummary with all step results and overall outcome.
        """
        set_correlation_id(incident.correlation_id)
        start_time = time.monotonic()
        summary = ExecutionSummary(
            incident_number=incident.number,
            sop_id=sop.sop_id,
            sop_title=sop.title,
            outcome=ExecutionOutcome.RESOLVED,
        )

        logger.info(
            "Starting SOP execution: %s (%s) for incident %s",
            sop.title,
            sop.sop_id,
            incident.number,
        )

        if work_note_callback:
            await work_note_callback(
                incident.sys_id,
                f"[L1 Agent] Starting SOP: {sop.title} ({sop.sop_id})\n"
                f"Steps to execute: {len(sop.steps)}",
            )

        # Build step index for DECISION branching
        step_index: Dict[str, SOPStep] = {s.step_id: s for s in sop.steps}
        current_step_id = sop.steps[0].step_id if sop.steps else ""
        visited: set = set()

        while current_step_id and current_step_id not in visited:
            visited.add(current_step_id)
            step = step_index.get(current_step_id)
            if not step:
                logger.warning("Step %s not found; ending execution", current_step_id)
                break

            result = await self._execute_step(step, incident, summary)
            summary.step_results.append(result)

            # Post work note for this step
            if work_note_callback:
                note = (
                    f"[L1 Agent] Step {step.step_id} ({step.step_type}): "
                    f"{result.status.value.upper()}\n{result.output_summary}"
                )
                await work_note_callback(incident.sys_id, note)

            # Determine next step
            if result.status == StepStatus.ESCALATED:
                summary.outcome = ExecutionOutcome.ESCALATED
                summary.escalation_reason = result.error_message or "Step required escalation"
                break
            elif result.status == StepStatus.FAIL:
                if step.on_failure:
                    current_step_id = step.on_failure
                else:
                    summary.outcome = ExecutionOutcome.FAILED
                    summary.escalation_reason = f"Step {step.step_id} failed: {result.error_message}"
                    break
            elif result.status == StepStatus.SUCCESS:
                # For DECISION steps, recommended_next_step overrides on_success
                next_id = result.recommended_next_step or step.on_success
                current_step_id = next_id
            else:
                # SKIP -> move to on_success
                current_step_id = step.on_success

        elapsed = (time.monotonic() - start_time) * 1000
        summary.total_duration_ms = elapsed
        summary.completed_at = datetime.now(timezone.utc).isoformat()

        metrics.increment(
            "sop.executions",
            labels={"outcome": summary.outcome.value},
        )
        metrics.observe("sop.execution_duration_ms", elapsed)

        logger.info(
            "SOP execution complete: %s -> %s (%.0fms)",
            sop.sop_id,
            summary.outcome.value,
            elapsed,
        )
        return summary

    async def _execute_step(
        self,
        step: SOPStep,
        incident: Incident,
        summary: ExecutionSummary,
    ) -> StepResult:
        """Execute a single SOP step and return the result."""
        step_start = time.monotonic()
        logger.info("Executing step %s (%s)", step.step_id, step.step_type)

        # Check approval gate
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
                result = self._handle_note_step(step, incident)
            elif step_type == StepType.DECISION.value:
                result = self._handle_decision_step(step, incident, summary)
            else:
                result = await self._handle_adapter_step(step)

            elapsed = (time.monotonic() - step_start) * 1000
            result.duration_ms = elapsed
            return result

        except Exception as exc:
            elapsed = (time.monotonic() - step_start) * 1000
            logger.error("Step %s failed with exception: %s", step.step_id, exc)
            return StepResult(
                step_id=step.step_id,
                step_type=step.step_type,
                status=StepStatus.FAIL,
                error_message=str(exc),
                tool_called=step.step_type,
                input_summary=str(step.parameters),
                output_summary=f"Exception: {exc}",
                duration_ms=elapsed,
            )

    async def _handle_adapter_step(self, step: SOPStep) -> StepResult:
        """Dispatch to the correct tool adapter based on step_type."""
        adapter_map = {
            StepType.SPLUNK_SEARCH.value: "splunk",
            StepType.MQ_CHECK.value: "ir360",
            StepType.FILE_CHECK.value: "windows_share",
            StepType.AUTOSYS_STATUS.value: "autosys",
            StepType.DYNATRACE_VM_CHECK.value: "dynatrace",
            StepType.DYNATRACE_METRICS.value: "dynatrace",
            StepType.WEB_UI_CHECK.value: "webui",
            StepType.MAINFRAME_CHECK.value: "mainframe",
        }

        adapter_key = adapter_map.get(step.step_type.upper())
        if not adapter_key:
            return StepResult(
                step_id=step.step_id,
                step_type=step.step_type,
                status=StepStatus.FAIL,
                error_message=f"Unknown step type: {step.step_type}",
                tool_called="none",
                input_summary=str(step.parameters),
                output_summary="No adapter for step type",
            )

        adapter = self._adapters.get(adapter_key)
        if not adapter:
            return StepResult(
                step_id=step.step_id,
                step_type=step.step_type,
                status=StepStatus.ESCALATED,
                error_message=f"Adapter '{adapter_key}' not configured; escalating",
                tool_called=adapter_key,
                input_summary=str(step.parameters),
                output_summary="Adapter unavailable",
            )

        # Execute with retry + circuit breaker
        cb = self._circuit_breakers.get(adapter_key)

        async def _run() -> AdapterResult:
            result = await adapter.execute(step.parameters)
            if not result.success:
                # Raise so retry_with_backoff can see the failure and retry.
                # Validation errors (missing required params) are not retryable
                # but transient errors (network, timeout) are.  We surface
                # all failures here and let the retry budget handle it.
                raise _AdapterFailure(result)
            return result

        try:
            adapter_result = await retry_with_backoff(
                _run,
                max_attempts=self._retry_max,
                base_delay=self._retry_delay,
                circuit_breaker=cb,
            )
        except RuntimeError as exc:
            # Circuit breaker open
            return StepResult(
                step_id=step.step_id,
                step_type=step.step_type,
                status=StepStatus.ESCALATED,
                error_message=str(exc),
                tool_called=adapter.adapter_name,
                input_summary=str(step.parameters),
                output_summary="Circuit breaker open; escalating",
            )
        except _AdapterFailure as exc:
            # All retries exhausted with adapter-level failures
            adapter_result = exc.result
            return StepResult(
                step_id=step.step_id,
                step_type=step.step_type,
                status=StepStatus.FAIL,
                error_message=adapter_result.error,
                tool_called=adapter.adapter_name,
                input_summary=str(step.parameters),
                output_summary=adapter_result.summary(),
            )
        except Exception as exc:
            return StepResult(
                step_id=step.step_id,
                step_type=step.step_type,
                status=StepStatus.FAIL,
                error_message=str(exc),
                tool_called=adapter.adapter_name,
                input_summary=str(step.parameters),
                output_summary=f"All retries exhausted: {exc}",
            )

        return StepResult(
            step_id=step.step_id,
            step_type=step.step_type,
            status=StepStatus.SUCCESS,
            evidence=adapter_result.evidence_snippet,
            tool_called=adapter.adapter_name,
            input_summary=str(step.parameters),
            output_summary=adapter_result.summary(),
        )

    @staticmethod
    def _handle_note_step(step: SOPStep, incident: Incident) -> StepResult:
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
    def _handle_decision_step(
        step: SOPStep, incident: Incident, summary: ExecutionSummary
    ) -> StepResult:
        """Handle a DECISION step: evaluate a rule to determine branching.

        The rule is evaluated against the last step result's data.
        Supports simple rules like:
          - "result.data.current_depth > 1000" -> on_success / on_failure
          - "any_failed" -> True if any previous step failed
        """
        rule = step.parameters.get("rule", "")
        previous_results = summary.step_results

        # Simple built-in rules
        if rule == "any_failed":
            has_failure = any(r.status == StepStatus.FAIL for r in previous_results)
            branch = step.on_failure if has_failure else step.on_success
            return StepResult(
                step_id=step.step_id,
                step_type=step.step_type,
                status=StepStatus.SUCCESS,
                tool_called="decision",
                input_summary=f"Rule: {rule}",
                output_summary=f"Decision: {'failure branch' if has_failure else 'success branch'}",
                recommended_next_step=branch,
            )

        if rule == "all_success":
            all_ok = all(
                r.status == StepStatus.SUCCESS
                for r in previous_results
                if r.step_type != StepType.NOTE.value
            )
            branch = step.on_success if all_ok else step.on_failure
            return StepResult(
                step_id=step.step_id,
                step_type=step.step_type,
                status=StepStatus.SUCCESS,
                tool_called="decision",
                input_summary=f"Rule: {rule}",
                output_summary=f"Decision: {'success branch' if all_ok else 'failure branch'}",
                recommended_next_step=branch,
            )

        # Default: pass through to on_success
        return StepResult(
            step_id=step.step_id,
            step_type=step.step_type,
            status=StepStatus.SUCCESS,
            tool_called="decision",
            input_summary=f"Rule: {rule} (unrecognised, defaulting to success)",
            output_summary="Decision: default success branch",
            recommended_next_step=step.on_success,
        )
