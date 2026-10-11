"""Function-calling schema for the one model task that exists: ranking SOP candidates.

The model is never offered operational tools. Capability calls are decided by
the published SOP and validated against src/l1_agent/adapters/schemas.py
(docs/adr/0001-one-execution-engine.md).

There is exactly one function and the request names it explicitly. Naming the
function is the most widely supported way to force a structured answer across
OpenAI-compatible providers: some ignore ``tool_choice="required"`` and write
the call as free text, which this code deliberately never parses.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

SELECT_SOP_FUNCTION = "select_sop"
# The value the model returns when none of the candidates fits.
NO_SOP = "NONE"


def get_sop_selection_tools(candidate_ids: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Return the tool definition for SOP selection.

    When ``candidate_ids`` is given, ``sop_id`` is constrained to those ids
    plus NO_SOP, so a provider that enforces schemas cannot return anything else.
    """
    sop_id: Dict[str, Any] = {
        "type": "string",
        "description": f'The ID of the selected SOP, or "{NO_SOP}" if no listed SOP is a good match.',
    }
    if candidate_ids is not None:
        sop_id["enum"] = [*candidate_ids, NO_SOP]
    return [
        {
            "type": "function",
            "function": {
                "name": SELECT_SOP_FUNCTION,
                "description": (
                    "Report which of the listed SOPs best fits this incident, or that none "
                    "does. Provide your reasoning."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sop_id": sop_id,
                        "confidence": {
                            "type": "number",
                            "description": "Confidence score from 0.0 to 1.0.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Reasoning for the choice, in one or two sentences.",
                        },
                    },
                    "required": ["sop_id", "confidence", "rationale"],
                },
            },
        },
    ]


def sop_selection_tool_choice() -> Dict[str, Any]:
    """The ``tool_choice`` value that forces a call to the selection function."""
    return {"type": "function", "function": {"name": SELECT_SOP_FUNCTION}}
