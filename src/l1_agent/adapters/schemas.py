"""Closed parameter schemas for adapter-backed step types.

Every adapter call is validated against these before ``execute`` runs. A key
that is not listed is rejected, so a caller cannot pass options an adapter
happens to read (for example mainframe keystrokes).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.l1_agent.engine.parameters import PLACEHOLDER_RE
from src.l1_agent.models.sop import StepType

# step type -> adapter registry key
STEP_ADAPTERS: Dict[str, str] = {
    StepType.SPLUNK_SEARCH.value: "splunk",
    StepType.MQ_CHECK.value: "ir360",
    StepType.FILE_CHECK.value: "windows_share",
    StepType.AUTOSYS_STATUS.value: "autosys",
    StepType.DYNATRACE_VM_CHECK.value: "dynatrace",
    StepType.DYNATRACE_METRICS.value: "dynatrace",
    StepType.WEB_UI_CHECK.value: "webui",
    StepType.MAINFRAME_CHECK.value: "mainframe",
}

_DYNATRACE = {
    "required": [],
    "properties": {
        "action": (str, ["vm_health", "metrics", "problems"]),
        "host_name": (str, None),
        "host_group": (str, None),
        "metric_selector": (str, None),
        "entity_selector": (str, None),
        "time_range": (str, None),
    },
}

# property -> (accepted type or tuple of types, allowed values or None)
STEP_PARAMETER_SCHEMAS: Dict[str, Dict[str, Any]] = {
    StepType.SPLUNK_SEARCH.value: {
        "required": ["query"],
        "properties": {
            "query": (str, None),
            "time_range": (dict, None),
            "max_results": (int, None),
        },
    },
    StepType.MQ_CHECK.value: {
        "required": ["queue_manager"],
        "properties": {
            "queue_manager": (str, None),
            "queue": (str, None),
            "action": (str, ["depth", "browse", "status"]),
        },
    },
    StepType.FILE_CHECK.value: {
        "required": ["unc_path"],
        "properties": {
            "unc_path": (str, None),
            "pattern": (str, None),
            "last_n_lines": (int, None),
        },
    },
    StepType.AUTOSYS_STATUS.value: {
        "required": ["job_name"],
        "properties": {
            "job_name": (str, None),
            "query_type": (str, ["status", "dependencies"]),
        },
    },
    StepType.DYNATRACE_VM_CHECK.value: _DYNATRACE,
    StepType.DYNATRACE_METRICS.value: _DYNATRACE,
    StepType.WEB_UI_CHECK.value: {
        "required": ["url"],
        "properties": {
            "url": (str, None),
            "check_type": (str, ["page_load", "click_path", "element_check"]),
            "click_steps": (list, None),
            "expected_text": (str, None),
            "expected_element": (str, None),
            "screenshot": (bool, None),
        },
    },
    StepType.MAINFRAME_CHECK.value: {
        "required": [],
        "properties": {
            "action": (str, ["async_status", "job_status", "screen_check"]),
            "job_name": (str, None),
            "transaction": (str, None),
            "expected_status": (str, None),
            "expected_text": (str, None),
        },
    },
}


def _has_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(PLACEHOLDER_RE.search(value))


def validate_step_parameters(
    step_type: str,
    parameters: Any,
    *,
    allow_placeholders: bool = False,
) -> List[str]:
    """Return a list of problems; an empty list means the parameters are valid.

    ``allow_placeholders`` is for static validation of an SOP, where a value
    may still be an unrendered ``${name}``.
    """
    schema: Optional[Dict[str, Any]] = STEP_PARAMETER_SCHEMAS.get(step_type)
    if schema is None:
        return [f"no parameter schema for step type '{step_type}'"]
    if not isinstance(parameters, dict):
        return ["parameters must be an object"]

    problems: List[str] = []
    properties: Dict[str, Tuple[Any, Optional[list]]] = schema["properties"]
    for key in parameters:
        if key not in properties:
            problems.append(f"parameter '{key}' is not allowed for {step_type}")
    for key in schema["required"]:
        if parameters.get(key) in (None, ""):
            problems.append(f"parameter '{key}' is required for {step_type}")
    for key, value in parameters.items():
        if key not in properties:
            continue
        if allow_placeholders and _has_placeholder(value):
            continue
        expected, allowed = properties[key]
        # bool is a subclass of int; do not accept True where a number is expected.
        if isinstance(value, bool) and expected is not bool:
            problems.append(f"parameter '{key}' has the wrong type")
        elif not isinstance(value, expected):
            problems.append(f"parameter '{key}' has the wrong type")
        elif allowed is not None and value not in allowed:
            problems.append(f"parameter '{key}' must be one of {allowed}")
    return problems
