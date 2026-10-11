"""The four things a model may be asked for (ADR 0009).

rank       which of the SOP candidates fits an incident best
extract    where in the ticket a named value is
interpret  a hand-over summary of what the checks found
draft      a draft SOP from a prose article, for a person to review

Each task has a prompt with a version and a closed schema for the answer. An
answer that does not fit the schema is discarded by the gateway. None of the
four can choose a tool, supply an argument that is not checked against the
ticket and the SOP author's pattern, change an outcome or cause a write: what
the engine does with each answer is decided in the engine, and
`evals/` measures it at the connector boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from src.l1_agent.ai.gateway import ModelGateway, Scope
from src.l1_agent.ai.prompts import DRAFT, EXTRACT, INTERPRET, PROMPTS, RANK
from src.l1_agent.ai.redaction import Redactor
from src.l1_agent.models.evidence import ExecutionSummary
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, ParameterSpec
from src.l1_agent.utils.logging import get_logger

logger = get_logger("advisor")

NO_SOP = "NONE"
MODEL_LABEL = "Model-drafted"
CAUSES = (
    "connectivity",
    "resource_exhaustion",
    "application_error",
    "configuration",
    "data",
    "scheduling",
    "unknown",
)
DRAFT_STEP_TYPES = ("CHECK", "DECISION", "NOTE", "VERIFY", "ESCALATE", "RESOLVE")
MAX_STEPS_SHOWN = 20
MAX_ARTICLE_CHARS = 16_000
# The most a model may write for each task. A model that reasons before it answers
# spends these tokens on the reasoning too, and an answer cut short is no answer.
OUTPUT_TOKENS = {RANK: 1200, EXTRACT: 800, INTERPRET: 1500, DRAFT: 4000}
_IDENT = r"[A-Za-z_][A-Za-z0-9_]{0,39}"
_STEP_ID = r"[A-Za-z0-9_\-]{1,40}"


def _text(max_length: int, **more: Any) -> Dict[str, Any]:
    return {"type": "string", "maxLength": max_length, **more}


def _object(properties: Dict[str, Any], required: Sequence[str] = ()) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
    }


def rank_schema(candidate_ids: Sequence[str]) -> Dict[str, Any]:
    return _object(
        {
            "sop_id": {"type": "string", "enum": [*candidate_ids, NO_SOP]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": _text(600),
        },
        required=["sop_id", "rationale"],
    )


def extract_schema(specs: Sequence[ParameterSpec]) -> Dict[str, Any]:
    return _object({spec.name: _text(max(1, min(spec.max_length, 500))) for spec in specs})


INTERPRET_SCHEMA = _object(
    {
        "summary": _text(800),
        "likely_cause": {"type": "string", "enum": list(CAUSES)},
        "next_checks": {"type": "array", "maxItems": 3, "items": _text(200)},
    },
    required=["summary", "likely_cause"],
)


def draft_schema(capability_ids: Sequence[str]) -> Dict[str, Any]:
    step = _object(
        {
            "step_id": _text(40, pattern=_STEP_ID),
            "type": {"type": "string", "enum": list(DRAFT_STEP_TYPES)},
            "description": _text(400),
            # For CHECK and VERIFY: one of the listed read capabilities.
            "capability": {"type": "string", "enum": [*capability_ids, ""]},
            # For CHECK and VERIFY: the capability's parameters, as a JSON object in a string.
            "arguments": _text(2000),
            "rule": _text(300),  # DECISION
            "assertion": _text(300),  # VERIFY
            "on_success": _text(40),
            "on_failure": _text(40),
        },
        required=["step_id", "type", "description"],
    )
    parameter = _object(
        {
            "name": _text(40, pattern=_IDENT),
            "description": _text(200),
            "regex": _text(200),
        },
        required=["name", "regex"],
    )
    return _object(
        {
            "keywords": {"type": "array", "maxItems": 12, "items": _text(60)},
            "parameters": {"type": "array", "maxItems": 8, "items": parameter},
            "steps": {"type": "array", "maxItems": 30, "items": step},
        },
        required=["steps"],
    )


@dataclass(frozen=True)
class Ranking:
    sop: Optional[SOP]  # None: the model found no candidate that fits
    rationale: str


@dataclass(frozen=True)
class Interpretation:
    summary: str
    likely_cause: str
    next_checks: List[str] = field(default_factory=list)

    def to_note(self) -> str:
        """The text shown to the engineer, labelled for what it is."""
        lines = [
            f"[L1 Agent] {MODEL_LABEL} summary. It was written by a language model from the "
            "results above. It has not been verified and was not used for any decision.",
            f"Likely kind of cause: {self.likely_cause.replace('_', ' ')}",
            self.summary,
        ]
        if self.next_checks:
            lines.append("Worth checking next:")
            lines += [f"  - {check}" for check in self.next_checks]
        return "\n".join(lines)


def _json_object(text: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text) if text else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def sop_content_from_draft(answer: Dict[str, Any]) -> Dict[str, Any]:
    """Keywords, parameters and steps in the SOP format, from the model's flat answer."""
    steps: List[Dict[str, Any]] = []
    for item in answer.get("steps", []):
        kind = item["type"]
        description = item.get("description", "")
        step: Dict[str, Any] = {
            "step_id": item["step_id"],
            "step_type": kind,
            "description": description,
            "on_success": item.get("on_success", ""),
            "on_failure": item.get("on_failure", ""),
        }
        arguments = _json_object(item.get("arguments", ""))
        if kind == "CHECK":
            step["capability"] = item.get("capability", "")
            step["parameters"] = arguments
        elif kind == "VERIFY":
            step["parameters"] = {
                "check": item.get("capability", ""),
                "parameters": arguments,
                "assert": item.get("assertion", ""),
            }
        elif kind == "DECISION":
            step["parameters"] = {"rule": item.get("rule", "")}
        elif kind == "ESCALATE":
            step["parameters"] = {"reason": description}
        else:  # NOTE, RESOLVE
            step["parameters"] = {"text": description}
        steps.append(step)
    parameters = [
        {
            "name": p["name"],
            "type": "string",
            "description": p.get("description", ""),
            "source": {
                "kind": "extraction",
                "fields": ["short_description", "description"],
                "regex": p["regex"],
            },
        }
        for p in answer.get("parameters", [])
    ]
    return {"keywords": answer.get("keywords", []), "parameters": parameters, "steps": steps}


class Advisor:
    """The four tasks for one run, or for one article. Every method may return nothing."""

    def __init__(self, gateway: ModelGateway, scope: Scope, redactor: Redactor) -> None:
        self._gateway = gateway
        self._scope = scope
        self._redactor = redactor

    @property
    def redactor(self) -> Redactor:
        return self._redactor

    async def rank(self, incident: Incident, candidates: Sequence[SOP]) -> Optional[Ranking]:
        """The candidate the model thinks fits best. The engine re-checks the pick."""
        if not candidates:
            return None
        listed = [
            {
                "sop_id": sop.sop_id,
                "title": sop.title,
                "keywords": sop.keywords,
                "applicable_services": sop.applicable_services,
                "applicable_categories": sop.applicable_categories,
                "applicable_assignment_groups": sop.applicable_assignment_groups,
            }
            for sop in candidates
        ]
        answer = await self._gateway.ask(
            self._scope,
            self._redactor,
            PROMPTS[RANK],
            trusted=(
                f"Incident {incident.number}, priority {incident.priority}.\n"
                f"Candidate SOPs:\n{json.dumps(listed, indent=2)}"
            ),
            sections=_ticket_sections(incident),
            parameters=rank_schema([sop.sop_id for sop in candidates]),
            max_output_tokens=OUTPUT_TOKENS[RANK],
        )
        if answer is None:
            return None
        picked = next((sop for sop in candidates if sop.sop_id == answer["sop_id"]), None)
        return Ranking(picked, answer["rationale"])

    async def extract(self, incident: Incident, specs: Sequence[ParameterSpec]) -> Dict[str, str]:
        """Where the model says each value is. The engine accepts none of it unchecked."""
        if not specs:
            return {}
        fields: List[str] = []
        for spec in specs:
            for name in spec.source.get("fields", ["short_description", "description"]):
                if name not in fields:
                    fields.append(name)
        wanted = "\n".join(
            f"- {spec.name}: {spec.description or 'no description'}"
            + (f" (shape: {spec.pattern})" if spec.pattern else "")
            for spec in specs
        )
        answer = await self._gateway.ask(
            self._scope,
            self._redactor,
            PROMPTS[EXTRACT],
            trusted=f"Values to locate in the ticket:\n{wanted}",
            sections=[(name, getattr(incident, name, "")) for name in fields],
            parameters=extract_schema(specs),
            max_output_tokens=OUTPUT_TOKENS[EXTRACT],
        )
        return {k: v for k, v in (answer or {}).items() if isinstance(v, str) and v}

    async def interpret(
        self, incident: Incident, summary: ExecutionSummary
    ) -> Optional[Interpretation]:
        """A hand-over summary of a run that did not resolve the incident."""
        results = summary.step_results[:MAX_STEPS_SHOWN]
        if not results:
            return None
        sections = _ticket_sections(incident)
        sections.append(("Why the run ended", summary.escalation_reason))
        for result in results:
            sections.append(
                (
                    f"Check {result.step_id} ({result.step_type}, {result.status.value})",
                    result.output_summary or result.error_message,
                )
            )
        answer = await self._gateway.ask(
            self._scope,
            self._redactor,
            PROMPTS[INTERPRET],
            trusted=(
                f"Incident {incident.number}. Procedure: {summary.sop_title} "
                f"({summary.sop_id}). It ended as: {summary.outcome_label}."
            ),
            sections=sections,
            parameters=INTERPRET_SCHEMA,
            max_output_tokens=OUTPUT_TOKENS[INTERPRET],
        )
        if answer is None:
            return None
        # The note goes back to the ticket the text came from, so names and addresses the
        # model was not shown are put back. Secrets stay masked.
        restore = self._redactor.restore
        return Interpretation(
            restore(answer["summary"]),
            answer["likely_cause"],
            [restore(check) for check in answer.get("next_checks", [])],
        )

    async def draft_sop(
        self, title: str, article_text: str, capabilities: Sequence[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Keywords, parameters and steps drafted from an article. Not yet validated."""
        if not article_text.strip() or not capabilities:
            return None
        answer = await self._gateway.ask(
            self._scope,
            self._redactor,
            PROMPTS[DRAFT],
            trusted=(
                "Read-only capabilities you may use in CHECK and VERIFY steps:\n"
                + json.dumps(list(capabilities), indent=2)
            ),
            sections=[("Article title", title), ("Article", article_text)],
            parameters=draft_schema([c["id"] for c in capabilities]),
            max_output_tokens=OUTPUT_TOKENS[DRAFT],
            section_chars=MAX_ARTICLE_CHARS,
        )
        return sop_content_from_draft(answer) if answer is not None else None


def _ticket_sections(incident: Incident) -> List[tuple]:
    """The ticket fields a model may see. The caller's name is never one of them."""
    return [
        ("Short description", incident.short_description),
        ("Description", incident.description),
        ("Category", incident.category),
        ("Subcategory", incident.subcategory),
        ("Configuration item", incident.cmdb_ci),
        ("Assignment group", incident.assignment_group),
    ]
