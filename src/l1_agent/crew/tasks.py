"""CrewAI task definitions for the L1 incident resolution pipeline.

build_tasks(incident, agents) returns a list of four Task objects in
execution order: triage → review → resolution → resolver. Each task
receives the outputs of prior tasks as context so agents can reason
across the full pipeline.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List


def build_tasks(incident: Any, agents: Dict[str, Any]) -> List[Any]:
    """Create the four sequential tasks for a given incident."""
    from crewai import Task

    incident_json = json.dumps(incident.to_dict(), indent=2)

    triage_task = Task(
        description=(
            "Analyze the following ServiceNow incident and produce a structured "
            "triage report.\n\n"
            "Extract and clearly state:\n"
            "- Incident number\n"
            "- Category and subcategory\n"
            "- CMDB CI (the affected system/host name)\n"
            "- Assignment group\n"
            "- Priority (1=Critical … 5=Planning)\n"
            "- Short description\n"
            "- Bullet list of key symptoms and indicators\n"
            "- Initial urgency assessment (Critical/High/Medium/Low)\n\n"
            f"Incident JSON:\n```json\n{incident_json}\n```"
        ),
        expected_output=(
            "A structured triage report containing: incident number, category, CMDB CI, "
            "assignment group, priority level, short description, a bullet list of "
            "symptoms, and an urgency assessment. Written in plain text — no code blocks."
        ),
        agent=agents["triage"],
    )

    review_task = Task(
        description=(
            "Using the triage report from the previous step, select the best matching "
            "Standard Operating Procedure (SOP) for this incident.\n\n"
            "Steps:\n"
            "1. Call list_sops to see all available runbooks\n"
            "2. Identify 1–2 candidates based on keywords, category, and CI name\n"
            "3. Call get_sop_details on each candidate to review their steps\n"
            "4. Select the best match and state:\n"
            "   - Selected SOP ID and title\n"
            "   - Confidence score (0.0–1.0)\n"
            "   - One-paragraph rationale explaining why this SOP matches\n"
            "   - The key investigation steps the ResolutionAgent should execute\n\n"
            "If no SOP matches with confidence ≥ 0.6, state:\n"
            "   'NO_SOP_MATCH: <reason>' and recommend L2 escalation."
        ),
        expected_output=(
            "Selected SOP ID and title, confidence score (e.g. 0.82), a rationale "
            "paragraph, and a summary of the key investigation steps. "
            "Or 'NO_SOP_MATCH: <reason>' if no SOP fits."
        ),
        agent=agents["review"],
        context=[triage_task],
    )

    resolution_task = Task(
        description=(
            "Using the triage report and the selected SOP from previous steps, "
            "execute the investigation.\n\n"
            "Instructions:\n"
            "- Follow the SOP step order as closely as possible\n"
            "- Call each tool specified in the SOP with the correct parameters\n"
            "  (use the CMDB CI / host name from the triage report)\n"
            "- After each tool call, interpret the result and decide whether "
            "  to continue, branch, or escalate\n"
            "- Record the finding from each tool call\n\n"
            "If the review agent found NO_SOP_MATCH, skip tool calls and "
            "output: 'OUTCOME: ESCALATED — Reason: No matching SOP found.'\n\n"
            "At the end state your determination:\n"
            "  OUTCOME: RESOLVED — <evidence summary>\n"
            "  or\n"
            "  OUTCOME: ESCALATED — Reason: <reason>. Evidence: <summary>\n"
        ),
        expected_output=(
            "A step-by-step investigation log with each tool called, the result "
            "obtained, and the interpretation. Ends with 'OUTCOME: RESOLVED' or "
            "'OUTCOME: ESCALATED — Reason: <reason>' on its own line."
        ),
        agent=agents["resolution"],
        context=[triage_task, review_task],
    )

    resolver_task = Task(
        description=(
            "Based on the triage report, SOP selection, and investigation findings "
            "from the previous agents, take the final action.\n\n"
            f"Incident number: {incident.number}\n\n"
            "If the investigation outcome is RESOLVED:\n"
            "  1. Write a clear work note summarising:\n"
            "     - What was checked (tools and results)\n"
            "     - Root cause or finding\n"
            "     - Why the incident is resolved\n"
            "  2. End your response with the line: OUTCOME: RESOLVED\n\n"
            "If the investigation outcome is ESCALATED:\n"
            "  1. Write a work note summarising:\n"
            "     - What was checked\n"
            "     - What evidence confirms escalation is needed\n"
            "     - Recommended L2 actions\n"
            "  2. End your response with the line: OUTCOME: ESCALATED\n\n"
            "Always include 'OUTCOME: RESOLVED' or 'OUTCOME: ESCALATED' "
            "as the very last line of your response."
        ),
        expected_output=(
            "A work note suitable for posting to ServiceNow, followed by the "
            "exact line 'OUTCOME: RESOLVED' or 'OUTCOME: ESCALATED'."
        ),
        agent=agents["resolver"],
        context=[triage_task, review_task, resolution_task],
    )

    return [triage_task, review_task, resolution_task, resolver_task]
