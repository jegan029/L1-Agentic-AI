"""Function-calling schema for the one model task that exists: ranking SOP candidates.

The model is never offered operational tools. Capability calls are decided by
the published SOP and validated against src/l1_agent/adapters/schemas.py
(docs/adr/0001-one-execution-engine.md).
"""

from __future__ import annotations

from typing import Any, Dict, List


def get_sop_selection_tools() -> List[Dict[str, Any]]:
    """Return tool definitions for SOP selection phase."""
    return [
        {
            "type": "function",
            "function": {
                "name": "select_sop",
                "description": (
                    "Select the most appropriate SOP for this incident. "
                    "Provide your reasoning for the selection."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sop_id": {
                            "type": "string",
                            "description": "The ID of the selected SOP.",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "Confidence score from 0.0 to 1.0.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Reasoning for why this SOP matches the incident.",
                        },
                    },
                    "required": ["sop_id", "confidence", "rationale"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "escalate_no_sop",
                "description": ("Escalate when no suitable SOP matches the incident."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Why no SOP matches.",
                        },
                    },
                    "required": ["reason"],
                },
            },
        },
    ]
