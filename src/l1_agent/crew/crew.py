"""Crew assembly and result parsing for the L1 incident resolution pipeline.

run_crew_for_incident() is the public entry point called by CrewIncidentProcessor.
It registers adapters/SOPs, builds agents+tasks, runs the crew synchronously,
and returns a standard ExecutionSummary that the rest of the system can consume
unchanged.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def run_crew_for_incident(
    incident: Any,
    crewai_settings: Any,
    sops: List[Any],
    adapters: Dict[str, Any],
) -> Any:
    """Run the four-agent CrewAI pipeline for a single incident.

    Returns an ExecutionSummary compatible with the existing history store,
    SSE publisher, and ServiceNow update logic.
    """
    from crewai import Crew, Process

    from src.l1_agent.crew.agents import build_agents
    from src.l1_agent.crew.tasks import build_tasks
    from src.l1_agent.crew.tools import register_adapters, register_sops

    # Inject adapters and SOPs into the module-level registries used by tools
    register_adapters(adapters)
    register_sops(sops)

    agents = build_agents(crewai_settings)
    tasks = build_tasks(incident, agents)

    crew = Crew(
        agents=list(agents.values()),
        tasks=tasks,
        process=Process.sequential,
        verbose=crewai_settings.verbose,
    )

    started = time.monotonic()
    crew_output = crew.kickoff(inputs={"incident_json": str(incident.to_dict())})
    elapsed_ms = (time.monotonic() - started) * 1000

    return _parse_crew_result(crew_output, tasks, incident, elapsed_ms)


def _parse_crew_result(crew_output: Any, tasks: List[Any], incident: Any, elapsed_ms: float) -> Any:
    """Convert CrewAI output into a standard ExecutionSummary."""
    from src.l1_agent.models.evidence import (
        ExecutionOutcome,
        ExecutionSummary,
        StepResult,
        StepStatus,
    )

    now = datetime.now(timezone.utc).isoformat()

    # ── Determine outcome from the resolver task's raw output ────────────────
    resolver_output: str = ""
    sop_id = "CREW-PIPELINE"
    sop_title = "CrewAI Multi-Agent Pipeline"

    try:
        tasks_output = crew_output.tasks_output if hasattr(crew_output, "tasks_output") else []
        if tasks_output:
            resolver_output = str(tasks_output[-1].raw if hasattr(tasks_output[-1], "raw") else tasks_output[-1])
            # Try to extract SOP from review task output (2nd task)
            if len(tasks_output) >= 2:
                review_raw = str(tasks_output[1].raw if hasattr(tasks_output[1], "raw") else tasks_output[1])
                sop_match = re.search(r"SOP[-\s]?ID[:\s]+([A-Z0-9\-]+)", review_raw, re.IGNORECASE)
                if sop_match:
                    sop_id = sop_match.group(1).strip()
                title_match = re.search(r"(?:Title|SOP)[:\s]+[A-Z0-9\-]+\s*[—–-]\s*(.+)", review_raw)
                if title_match:
                    sop_title = title_match.group(1).strip()
        else:
            resolver_output = str(crew_output.raw if hasattr(crew_output, "raw") else crew_output)
    except Exception:
        resolver_output = str(crew_output)

    # Parse OUTCOME keyword from the final output
    outcome = _extract_outcome(resolver_output)
    escalation_reason = ""
    if outcome == ExecutionOutcome.ESCALATED:
        esc_match = re.search(r"(?:Reason|ESCALATED)[:\s—–-]+(.+?)(?:\n|$)", resolver_output, re.IGNORECASE)
        if esc_match:
            escalation_reason = esc_match.group(1).strip()
        else:
            escalation_reason = "Escalated by CrewAI resolver agent"

    # ── Build one StepResult per agent task ──────────────────────────────────
    agent_labels = [
        ("crew-triage", "CREW_TRIAGE", "TriageAgent"),
        ("crew-review", "CREW_REVIEW", "ReviewAgent"),
        ("crew-resolution", "CREW_RESOLUTION", "ResolutionAgent"),
        ("crew-resolver", "CREW_RESOLVER", "ResolverAgent"),
    ]

    step_results: List[StepResult] = []
    try:
        tasks_output = crew_output.tasks_output if hasattr(crew_output, "tasks_output") else []
        for i, (step_id, step_type, tool_name) in enumerate(agent_labels):
            raw_output = ""
            if i < len(tasks_output):
                raw_output = str(
                    tasks_output[i].raw if hasattr(tasks_output[i], "raw") else tasks_output[i]
                )
            status = (
                StepStatus.ESCALATED
                if outcome == ExecutionOutcome.ESCALATED and i == len(agent_labels) - 1
                else StepStatus.SUCCESS
            )
            step_results.append(
                StepResult(
                    step_id=step_id,
                    step_type=step_type,
                    status=status,
                    evidence=raw_output,
                    tool_called=tool_name,
                    input_summary=f"Agent: {tool_name}",
                    output_summary=raw_output[:300] if raw_output else "(no output)",
                    timestamp=now,
                    duration_ms=elapsed_ms / len(agent_labels),
                )
            )
    except Exception:
        step_results.append(
            StepResult(
                step_id="crew-pipeline",
                step_type="CREW_PIPELINE",
                status=StepStatus.SUCCESS if outcome == ExecutionOutcome.RESOLVED else StepStatus.FAIL,
                evidence=resolver_output,
                tool_called="CrewAI",
                output_summary=resolver_output[:300],
                timestamp=now,
                duration_ms=elapsed_ms,
            )
        )

    return ExecutionSummary(
        incident_number=incident.number,
        sop_id=sop_id,
        sop_title=sop_title,
        outcome=outcome,
        step_results=step_results,
        escalation_reason=escalation_reason,
        started_at=now,
        completed_at=datetime.now(timezone.utc).isoformat(),
        total_duration_ms=elapsed_ms,
    )


def _extract_outcome(text: str) -> Any:
    """Parse the OUTCOME keyword from resolver agent output."""
    from src.l1_agent.models.evidence import ExecutionOutcome

    upper = text.upper()
    # Look for explicit OUTCOME: keyword first (most reliable)
    if re.search(r"OUTCOME\s*:\s*RESOLVED", upper):
        return ExecutionOutcome.RESOLVED
    if re.search(r"OUTCOME\s*:\s*ESCALATED", upper):
        return ExecutionOutcome.ESCALATED
    # Fallback: scan for bare keywords near end of text
    tail = upper[-500:]
    if "RESOLVED" in tail and "ESCALATED" not in tail:
        return ExecutionOutcome.RESOLVED
    if "ESCALATED" in tail:
        return ExecutionOutcome.ESCALATED
    return ExecutionOutcome.FAILED
