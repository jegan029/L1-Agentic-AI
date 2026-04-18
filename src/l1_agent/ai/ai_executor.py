"""AI-powered SOP executor: uses LLM to drive tool calls and interpret results.

Instead of mechanically walking through SOP steps, the AI executor sends
the incident context + SOP instructions to the LLM. The LLM decides which
tools to call, interprets the outputs, and determines whether to escalate
or resolve the incident.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.ai.llm_client import LLMClient
from src.l1_agent.ai.tool_definitions import get_investigation_tools
from src.l1_agent.models.evidence import (
    ExecutionOutcome,
    ExecutionSummary,
    StepResult,
    StepStatus,
)
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.utils.logging import get_logger, set_correlation_id
from src.l1_agent.utils.metrics import metrics

logger = get_logger("ai_executor")


class _TerminalToolAction(BaseException):
    """Raised by tool_executor when a terminal action (resolve/escalate) is called.

    Inherits from BaseException (not Exception) so that the ``except Exception``
    handler inside ``chat_with_tools`` does not swallow it.  This allows the
    exception to propagate up to ``execute_sop`` where it is explicitly caught,
    stopping the tool-calling loop immediately and preventing the LLM from
    overwriting the outcome with subsequent tool calls.
    """

    def __init__(self, result_json: str) -> None:
        self.result_json = result_json
        super().__init__("terminal tool action")

# ── System prompt for the execution phase ────────────────────────────

EXECUTION_SYSTEM_PROMPT = """\
You are an L1 Virtual Engineer Agent executing a Standard Operating Procedure \
(SOP) to investigate and resolve a ServiceNow incident.

Your role:
1. Follow the SOP steps to investigate the incident.
2. Use the available tools to gather evidence (logs, queue status, job status).
3. Analyze each tool's output to understand what it reveals about the incident.
4. Post work notes to keep the incident updated with your findings.
5. After completing all investigation steps, either:
   - Call resolve_incident if the investigation is complete and you have \
     enough evidence to summarize findings.
   - Call escalate_to_l2 if you find issues that require L2 intervention \
     (write actions needed, access denied, ambiguous results, etc.).

Rules:
- ONLY use tools listed in the SOP. Do not invent steps.
- ALWAYS post a work note after each significant finding.
- Be thorough: execute ALL relevant SOP steps before concluding.
- Be safe: NEVER perform destructive actions. All checks are read-only.
- Include evidence snippets in your work notes.
- If a tool call fails, note the failure and continue with remaining steps.
- End your investigation by calling either resolve_incident or escalate_to_l2.
"""


# Type alias for work-note callback
WorkNoteCallback = Callable[[str, str], Coroutine[Any, Any, Any]]


class AIExecutor:
    """LLM-driven SOP executor that uses tool calling for investigation.

    The LLM receives the incident + SOP context and autonomously decides
    which tools to call, interprets results, and determines the outcome.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        adapters: Dict[str, BaseAdapter],
        max_tool_iterations: int = 15,
    ) -> None:
        self._llm = llm_client
        self._adapters = adapters
        self._max_iterations = max_tool_iterations

    async def execute_sop(
        self,
        incident: Incident,
        sop: SOP,
        work_note_callback: Optional[WorkNoteCallback] = None,
    ) -> ExecutionSummary:
        """Execute an SOP using AI-driven tool calling.

        The LLM receives the full incident + SOP context and autonomously
        calls tools, interprets results, and decides the outcome.

        Args:
            incident: The incident being processed.
            sop: The matched SOP to execute.
            work_note_callback: Async callable(sys_id, note) for posting work notes.

        Returns:
            ExecutionSummary with all step results and overall outcome.
        """
        set_correlation_id(incident.correlation_id)
        start_time = time.monotonic()
        step_results: List[StepResult] = []

        logger.info(
            "AI executor starting SOP: %s for incident %s",
            sop.title,
            incident.number,
        )

        # Build the conversation context
        user_prompt = self._build_execution_prompt(incident, sop)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": EXECUTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        tools = get_investigation_tools()

        # State tracking
        outcome = ExecutionOutcome.RESOLVED
        escalation_reason = ""
        work_notes_posted: List[str] = []
        resolution_summary = ""

        # Create the tool executor closure
        async def tool_executor(
            tool_name: str, arguments: Dict[str, Any]
        ) -> str:
            nonlocal outcome, escalation_reason, resolution_summary

            step_start = time.monotonic()
            logger.info("AI tool call: %s(%s)", tool_name, json.dumps(arguments)[:200])

            # Handle action tools (not adapter-backed)
            if tool_name == "post_work_note":
                note_text = arguments.get("note", "")
                work_notes_posted.append(note_text)
                if work_note_callback:
                    await work_note_callback(
                        incident.sys_id, f"[L1 Agent - AI] {note_text}"
                    )
                _record_step(
                    step_results,
                    step_id=f"note-{len(work_notes_posted)}",
                    step_type="NOTE",
                    status=StepStatus.SUCCESS,
                    tool_called="post_work_note",
                    input_summary=note_text[:100],
                    output_summary="Work note posted",
                    evidence=note_text,
                    duration_ms=(time.monotonic() - step_start) * 1000,
                )
                return json.dumps({"status": "posted", "note_id": len(work_notes_posted)})

            if tool_name == "escalate_to_l2":
                reason = arguments.get("reason", "")
                findings = arguments.get("findings_summary", "")
                recommended = arguments.get("recommended_actions", "")
                outcome = ExecutionOutcome.ESCALATED
                escalation_reason = reason
                escalation_note = (
                    f"ESCALATION TO L2\n"
                    f"Reason: {reason}\n"
                    f"Findings: {findings}\n"
                    f"Recommended actions: {recommended}"
                )
                work_notes_posted.append(escalation_note)
                if work_note_callback:
                    await work_note_callback(
                        incident.sys_id, f"[L1 Agent - AI] {escalation_note}"
                    )
                _record_step(
                    step_results,
                    step_id="escalation",
                    step_type="ESCALATION",
                    status=StepStatus.ESCALATED,
                    tool_called="escalate_to_l2",
                    input_summary=reason[:100],
                    output_summary=escalation_note[:200],
                    evidence=escalation_note,
                    duration_ms=(time.monotonic() - step_start) * 1000,
                )
                raise _TerminalToolAction(
                    json.dumps({"status": "escalated", "reason": reason})
                )

            if tool_name == "resolve_incident":
                resolution_summary = arguments.get("resolution_summary", "")
                evidence_summary = arguments.get("evidence_summary", "")
                outcome = ExecutionOutcome.RESOLVED
                resolve_note = (
                    f"RESOLVED\n"
                    f"Summary: {resolution_summary}\n"
                    f"Evidence: {evidence_summary}"
                )
                work_notes_posted.append(resolve_note)
                if work_note_callback:
                    await work_note_callback(
                        incident.sys_id, f"[L1 Agent - AI] {resolve_note}"
                    )
                _record_step(
                    step_results,
                    step_id="resolution",
                    step_type="RESOLUTION",
                    status=StepStatus.SUCCESS,
                    tool_called="resolve_incident",
                    input_summary=resolution_summary[:100],
                    output_summary=resolve_note[:200],
                    evidence=resolve_note,
                    duration_ms=(time.monotonic() - step_start) * 1000,
                )
                raise _TerminalToolAction(
                    json.dumps({"status": "resolved", "summary": resolution_summary})
                )

            # Handle adapter-backed tools
            adapter_key = _tool_to_adapter_key(tool_name)
            adapter = self._adapters.get(adapter_key) if adapter_key else None

            # Inject action parameter for tools that share an adapter
            arguments = _inject_action(tool_name, arguments)

            if not adapter:
                error_msg = f"Tool '{tool_name}' not available (no adapter configured)"
                _record_step(
                    step_results,
                    step_id=f"tool-{tool_name}-{len(step_results)}",
                    step_type=tool_name.upper(),
                    status=StepStatus.FAIL,
                    tool_called=tool_name,
                    input_summary=json.dumps(arguments)[:200],
                    output_summary=error_msg,
                    error_message=error_msg,
                    duration_ms=(time.monotonic() - step_start) * 1000,
                )
                return json.dumps({"error": error_msg})

            try:
                result = await adapter.execute(arguments)
                elapsed = (time.monotonic() - step_start) * 1000

                if result.success:
                    _record_step(
                        step_results,
                        step_id=f"tool-{tool_name}-{len(step_results)}",
                        step_type=tool_name.upper(),
                        status=StepStatus.SUCCESS,
                        tool_called=adapter.adapter_name,
                        input_summary=json.dumps(arguments)[:200],
                        output_summary=result.summary()[:300],
                        evidence=result.evidence_snippet,
                        duration_ms=elapsed,
                    )
                    return json.dumps({
                        "status": "success",
                        "data": result.data,
                        "evidence": result.evidence_snippet,
                    })
                else:
                    _record_step(
                        step_results,
                        step_id=f"tool-{tool_name}-{len(step_results)}",
                        step_type=tool_name.upper(),
                        status=StepStatus.FAIL,
                        tool_called=adapter.adapter_name,
                        input_summary=json.dumps(arguments)[:200],
                        output_summary=result.summary()[:300],
                        error_message=result.error,
                        duration_ms=elapsed,
                    )
                    return json.dumps({
                        "status": "error",
                        "error": result.error,
                    })

            except Exception as exc:
                elapsed = (time.monotonic() - step_start) * 1000
                error_msg = f"Tool execution error: {exc}"
                _record_step(
                    step_results,
                    step_id=f"tool-{tool_name}-{len(step_results)}",
                    step_type=tool_name.upper(),
                    status=StepStatus.FAIL,
                    tool_called=tool_name,
                    input_summary=json.dumps(arguments)[:200],
                    output_summary=error_msg[:200],
                    error_message=str(exc),
                    duration_ms=elapsed,
                )
                return json.dumps({"error": error_msg})

        # Run the AI tool-calling loop
        try:
            await self._llm.chat_with_tools(
                messages=messages,
                tools=tools,
                tool_executor=tool_executor,
                max_iterations=self._max_iterations,
            )
        except _TerminalToolAction:
            # Expected: resolve_incident or escalate_to_l2 was called.
            # outcome/escalation_reason already set by the closure.
            pass
        except Exception as exc:
            logger.error("AI execution loop failed: %s", exc)
            outcome = ExecutionOutcome.FAILED
            escalation_reason = f"AI execution error: {exc}"

        # Build summary
        elapsed_total = (time.monotonic() - start_time) * 1000
        summary = ExecutionSummary(
            incident_number=incident.number,
            sop_id=sop.sop_id,
            sop_title=sop.title,
            outcome=outcome,
            step_results=list(step_results),
            escalation_reason=escalation_reason,
            completed_at=datetime.now(timezone.utc).isoformat(),
            total_duration_ms=elapsed_total,
        )

        metrics.increment(
            "sop.ai_executions",
            labels={"outcome": summary.outcome.value},
        )
        metrics.observe("sop.ai_execution_duration_ms", elapsed_total)

        logger.info(
            "AI SOP execution complete: %s -> %s (%.0fms, %d steps)",
            sop.sop_id,
            summary.outcome.value,
            elapsed_total,
            len(step_results),
        )
        return summary

    # ── Internal helpers ──────────────────────────────────────────────

    def _build_execution_prompt(self, incident: Incident, sop: SOP) -> str:
        """Build the user prompt with full incident and SOP context."""
        steps_desc = []
        for step in sop.steps:
            step_info = (
                f"  - Step {step.step_id} ({step.step_type}): "
                f"{step.description}"
            )
            if step.parameters:
                step_info += f"\n    Parameters: {json.dumps(step.parameters)}"
            if step.expected_output:
                step_info += f"\n    Expected: {step.expected_output}"
            if step.requires_approval:
                step_info += "\n    ** REQUIRES APPROVAL - ESCALATE **"
            steps_desc.append(step_info)

        return (
            "=== INCIDENT TO INVESTIGATE ===\n"
            f"Number: {incident.number}\n"
            f"Short Description: {incident.short_description}\n"
            f"Description: {incident.description}\n"
            f"Category: {incident.category} / {incident.subcategory}\n"
            f"CI: {incident.cmdb_ci}\n"
            f"Assignment Group: {incident.assignment_group}\n"
            f"Priority: {incident.priority}\n\n"
            "=== SOP TO EXECUTE ===\n"
            f"SOP: {sop.title} ({sop.sop_id})\n"
            f"Tools required: {', '.join(sop.tools_required)}\n"
            f"Pre-checks: {', '.join(sop.pre_checks)}\n\n"
            "Steps:\n"
            + "\n".join(steps_desc)
            + "\n\n"
            "=== INSTRUCTIONS ===\n"
            "Execute the SOP steps above using the available tools. "
            "Post work notes to document your findings after each "
            "significant check. After completing all steps, call either "
            "resolve_incident or escalate_to_l2 based on your findings."
        )


def _record_step(
    results: List[StepResult],
    step_id: str,
    step_type: str,
    status: StepStatus,
    tool_called: str,
    input_summary: str,
    output_summary: str,
    evidence: str = "",
    error_message: str = "",
    duration_ms: float = 0.0,
) -> None:
    """Record a step result for the execution summary."""
    results.append(
        StepResult(
            step_id=step_id,
            step_type=step_type,
            status=status,
            evidence=evidence,
            error_message=error_message,
            tool_called=tool_called,
            input_summary=input_summary,
            output_summary=output_summary,
            duration_ms=duration_ms,
        )
    )


def _tool_to_adapter_key(tool_name: str) -> Optional[str]:
    """Map LLM tool names to adapter dictionary keys."""
    mapping = {
        "splunk_search": "splunk",
        "mq_check": "ir360",
        "file_check": "windows_share",
        "autosys_status": "autosys",
        "dynatrace_vm_check": "dynatrace",
        "dynatrace_metrics_check": "dynatrace",
        "web_ui_check": "webui",
        "mainframe_async_check": "mainframe",
    }
    return mapping.get(tool_name)


def _inject_action(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Inject the correct 'action' parameter for tools that share an adapter.

    For example, dynatrace_vm_check and dynatrace_metrics_check both route
    to the DynatraceAdapter but need different action values.
    """
    action_map = {
        "dynatrace_vm_check": "vm_health",
        "dynatrace_metrics_check": "metrics",
    }
    action = action_map.get(tool_name)
    if action and "action" not in arguments:
        arguments = {**arguments, "action": action}
    return arguments
