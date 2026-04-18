"""AI-powered incident analyzer: uses LLM to understand tickets and select SOPs.

The analyzer replaces the rule-based SOP matcher with LLM reasoning.
It sends the incident details and available SOPs to the LLM, which then
selects the most appropriate SOP with a confidence score and rationale.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.l1_agent.ai.llm_client import LLMClient, LLMResponse
from src.l1_agent.ai.tool_definitions import get_sop_selection_tools
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.utils.logging import get_logger

logger = get_logger("ai_analyzer")


@dataclass
class AIMatchResult:
    """Result of AI-powered SOP matching."""

    sop: Optional[SOP]
    confidence: float
    rationale: str
    raw_llm_response: str = ""


# ── System prompts ───────────────────────────────────────────────────

SOP_SELECTION_SYSTEM_PROMPT = """\
You are an L1 Virtual Engineer Agent responsible for triaging incidents.

Your task: Given an incident from ServiceNow and a list of available SOPs \
(Standard Operating Procedures), select the most appropriate SOP to \
execute for this incident.

Rules:
- Analyze the incident short_description, description, category, CI, \
and assignment_group.
- Compare these fields against each SOP's title, keywords, applicable \
services, categories, and assignment groups.
- Select the SOP that best matches the incident.
- Assign a confidence score from 0.0 to 1.0 based on how well the SOP \
matches.
- Provide clear rationale for your selection.
- If NO SOP is a good match (confidence < 0.4), call escalate_no_sop \
instead of select_sop.
- Never invent steps or SOPs. Only select from the provided list.
"""


class AIAnalyzer:
    """Uses LLM to analyze incidents, select SOPs, and drive decision-making."""

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def select_sop(
        self,
        incident: Incident,
        available_sops: List[SOP],
    ) -> AIMatchResult:
        """Use the LLM to select the best SOP for the given incident.

        Args:
            incident: The ServiceNow incident to analyze.
            available_sops: List of available SOPs to choose from.

        Returns:
            AIMatchResult with the selected SOP, confidence, and rationale.
        """
        if not available_sops:
            return AIMatchResult(
                sop=None,
                confidence=0.0,
                rationale="No SOPs available in the knowledge base.",
            )

        # Build the prompt with incident and SOP details
        user_prompt = self._build_sop_selection_prompt(incident, available_sops)

        messages = [
            {"role": "system", "content": SOP_SELECTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        tools = get_sop_selection_tools()

        try:
            response = await self._llm.chat(
                messages=messages,
                tools=tools,
                tool_choice="required",
            )
            return self._parse_sop_selection_response(
                response, available_sops
            )
        except Exception as exc:
            logger.error("LLM SOP selection failed: %s", exc)
            return AIMatchResult(
                sop=None,
                confidence=0.0,
                rationale=f"AI analysis failed: {exc}",
            )

    async def analyze_incident(self, incident: Incident) -> str:
        """Use the LLM to produce a preliminary analysis of the incident.

        Returns a text summary of the AI's understanding of the incident,
        including likely root cause category and severity assessment.
        """
        prompt = (
            "Analyze this ServiceNow incident and provide a brief assessment:\n\n"
            f"Number: {incident.number}\n"
            f"Short Description: {incident.short_description}\n"
            f"Description: {incident.description}\n"
            f"Category: {incident.category} / {incident.subcategory}\n"
            f"CI: {incident.cmdb_ci}\n"
            f"Assignment Group: {incident.assignment_group}\n"
            f"Priority: {incident.priority}\n\n"
            "Provide:\n"
            "1. Likely root cause category (e.g., connectivity, resource exhaustion, "
            "application error, configuration)\n"
            "2. Key indicators from the description\n"
            "3. Recommended investigation areas\n"
            "Keep your response concise (under 200 words)."
        )
        try:
            return await self._llm.ask(prompt)
        except Exception as exc:
            logger.error("LLM incident analysis failed: %s", exc)
            return f"AI analysis unavailable: {exc}"

    async def interpret_tool_result(
        self,
        tool_name: str,
        tool_output: str,
        incident_context: str,
        sop_step_description: str,
    ) -> str:
        """Use the LLM to interpret a tool result in the context of the incident.

        Returns a concise interpretation of what the tool output means for
        the investigation.
        """
        prompt = (
            f"You are investigating an incident. Here is the context:\n\n"
            f"Incident: {incident_context}\n\n"
            f"SOP step: {sop_step_description}\n\n"
            f"Tool used: {tool_name}\n"
            f"Tool output:\n{tool_output}\n\n"
            "Interpret this result:\n"
            "1. What does this output tell us about the incident?\n"
            "2. Are there any anomalies or concerning findings?\n"
            "3. What should be the next action?\n"
            "Be concise (under 150 words)."
        )
        try:
            return await self._llm.ask(prompt)
        except Exception as exc:
            logger.error("LLM result interpretation failed: %s", exc)
            return f"AI interpretation unavailable: {exc}"

    # ── Internal helpers ──────────────────────────────────────────────

    def _build_sop_selection_prompt(
        self, incident: Incident, sops: List[SOP]
    ) -> str:
        """Build the user prompt for SOP selection."""
        sop_summaries = []
        for sop in sops:
            summary = {
                "sop_id": sop.sop_id,
                "title": sop.title,
                "keywords": sop.keywords,
                "applicable_services": sop.applicable_services,
                "applicable_categories": sop.applicable_categories,
                "applicable_assignment_groups": sop.applicable_assignment_groups,
                "tools_required": sop.tools_required,
                "step_count": len(sop.steps),
            }
            sop_summaries.append(summary)

        return (
            "=== INCIDENT ===\n"
            f"Number: {incident.number}\n"
            f"Short Description: {incident.short_description}\n"
            f"Description: {incident.description}\n"
            f"Category: {incident.category}\n"
            f"Subcategory: {incident.subcategory}\n"
            f"CI (Configuration Item): {incident.cmdb_ci}\n"
            f"Assignment Group: {incident.assignment_group}\n"
            f"Priority: {incident.priority}\n\n"
            "=== AVAILABLE SOPs ===\n"
            f"{json.dumps(sop_summaries, indent=2)}\n\n"
            "Select the best matching SOP or escalate if none match."
        )

    def _parse_sop_selection_response(
        self,
        response: LLMResponse,
        available_sops: List[SOP],
    ) -> AIMatchResult:
        """Parse the LLM's SOP selection tool call response."""
        sop_index: Dict[str, SOP] = {s.sop_id: s for s in available_sops}

        for tc in response.tool_calls:
            args = tc.arguments

            if tc.function_name == "select_sop":
                sop_id = args.get("sop_id", "")
                confidence = float(args.get("confidence", 0.0))
                rationale = args.get("rationale", "")
                selected_sop = sop_index.get(sop_id)

                if not selected_sop:
                    logger.warning(
                        "LLM selected unknown SOP ID: %s", sop_id
                    )
                    return AIMatchResult(
                        sop=None,
                        confidence=0.0,
                        rationale=f"AI selected unknown SOP: {sop_id}",
                        raw_llm_response=response.content,
                    )

                return AIMatchResult(
                    sop=selected_sop,
                    confidence=confidence,
                    rationale=rationale,
                    raw_llm_response=response.content,
                )

            elif tc.function_name == "escalate_no_sop":
                reason = args.get("reason", "No suitable SOP found")
                return AIMatchResult(
                    sop=None,
                    confidence=0.0,
                    rationale=reason,
                    raw_llm_response=response.content,
                )

        # Fallback: no tool calls
        return AIMatchResult(
            sop=None,
            confidence=0.0,
            rationale=f"AI did not select an SOP. Response: {response.content[:300]}",
            raw_llm_response=response.content,
        )
