"""Typed SOP parameters: binding from the ticket, validation and template rendering.

Replaces the unsubstituted ``${name}`` placeholders. A parameter that cannot
be bound or fails validation stops the run before any step executes.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Set

from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, ParameterSpec

PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Applied to every string parameter that does not declare its own pattern.
DEFAULT_STRING_PATTERN = r"[A-Za-z0-9_.\-]+"
# Ticket text is untrusted; cap what an extraction pattern is run against.
MAX_EXTRACTION_INPUT = 10_000
TICKET_FIELDS = frozenset(
    {
        "number",
        "short_description",
        "description",
        "category",
        "subcategory",
        "cmdb_ci",
        "assignment_group",
        "priority",
    }
)
SOURCE_KINDS = frozenset({"ticket_field", "extraction", "constant"})
PARAMETER_TYPES = frozenset({"string", "integer"})


class ParameterError(ValueError):
    """A parameter could not be bound or failed validation."""


def spec_problems(spec: ParameterSpec) -> List[str]:
    """Static checks on a parameter declaration, used by the SOP validator."""
    problems: List[str] = []
    label = f"parameter '{spec.name}'"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", spec.name or ""):
        problems.append(f"{label}: name must be an identifier")
    if spec.type not in PARAMETER_TYPES:
        problems.append(f"{label}: unknown type '{spec.type}'")
    kind = spec.source.get("kind") if isinstance(spec.source, dict) else None
    if kind not in SOURCE_KINDS:
        problems.append(f"{label}: source.kind must be one of {sorted(SOURCE_KINDS)}")
    elif kind == "ticket_field":
        if spec.source.get("field") not in TICKET_FIELDS:
            problems.append(f"{label}: source.field must be one of {sorted(TICKET_FIELDS)}")
    elif kind == "extraction":
        fields = spec.source.get("fields", ["short_description", "description"])
        if not fields or any(f not in TICKET_FIELDS for f in fields):
            problems.append(f"{label}: source.fields must name ticket fields")
        try:
            compiled = re.compile(spec.source.get("regex", ""))
            if compiled.groups != 1:
                problems.append(f"{label}: source.regex must have exactly one capture group")
        except re.error as exc:
            problems.append(f"{label}: source.regex is invalid: {exc}")
    elif kind == "constant" and "value" not in spec.source:
        problems.append(f"{label}: constant source needs a value")
    if spec.pattern:
        try:
            re.compile(spec.pattern)
        except re.error as exc:
            problems.append(f"{label}: pattern is invalid: {exc}")
    return problems


def _raw_value(spec: ParameterSpec, incident: Incident) -> Any:
    kind = spec.source.get("kind")
    if kind == "constant":
        return spec.source.get("value")
    if kind == "ticket_field":
        return getattr(incident, spec.source["field"], None)
    if kind == "extraction":
        fields = spec.source.get("fields", ["short_description", "description"])
        text = "\n".join(str(getattr(incident, f, "") or "") for f in fields)
        match = re.search(spec.source["regex"], text[:MAX_EXTRACTION_INPUT])
        return match.group(1) if match else None
    raise ParameterError(f"parameter '{spec.name}' has an unknown source")


def _validate(spec: ParameterSpec, value: Any) -> Any:
    if value is None or value == "":
        raise ParameterError(f"parameter '{spec.name}' could not be found in the ticket")
    if spec.type == "integer":
        try:
            value = int(str(value).strip())
        except ValueError:
            raise ParameterError(f"parameter '{spec.name}' is not an integer") from None
    else:
        value = str(value).strip()
        if len(value) > spec.max_length:
            raise ParameterError(f"parameter '{spec.name}' is longer than {spec.max_length}")
        if not re.fullmatch(spec.pattern or DEFAULT_STRING_PATTERN, value):
            raise ParameterError(f"parameter '{spec.name}' has a value that is not allowed")
    if spec.enum and value not in spec.enum:
        raise ParameterError(f"parameter '{spec.name}' is not one of the allowed values")
    return value


def bind_parameters(sop: SOP, incident: Incident) -> Dict[str, Any]:
    """Bind and validate every declared parameter. Raises ParameterError."""
    values: Dict[str, Any] = {}
    for spec in sop.parameters:
        values[spec.name] = _validate(spec, _raw_value(spec, incident))
    return values


def placeholders_in(obj: Any) -> Set[str]:
    """Collect every ``${name}`` used anywhere in a nested structure."""
    found: Set[str] = set()
    if isinstance(obj, str):
        found.update(PLACEHOLDER_RE.findall(obj))
    elif isinstance(obj, dict):
        for value in obj.values():
            found |= placeholders_in(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            found |= placeholders_in(value)
    return found


def render(obj: Any, values: Dict[str, Any]) -> Any:
    """Substitute placeholders in a nested structure.

    A string that is exactly one placeholder keeps the value's type; inside a
    longer string the value is inserted as text. Raises ParameterError for a
    placeholder with no bound value.
    """
    if isinstance(obj, str):
        whole = PLACEHOLDER_RE.fullmatch(obj)
        if whole:
            return _lookup(whole.group(1), values)
        return PLACEHOLDER_RE.sub(lambda m: str(_lookup(m.group(1), values)), obj)
    if isinstance(obj, dict):
        return {k: render(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render(v, values) for v in obj]
    return obj


def _lookup(name: str, values: Dict[str, Any]) -> Any:
    if name not in values:
        raise ParameterError(f"parameter '{name}' is used but was not bound")
    return values[name]


def undeclared_placeholders(sop: SOP) -> Iterable[str]:
    declared = {p.name for p in sop.parameters}
    used: Set[str] = set()
    for step in sop.steps:
        used |= placeholders_in(step.parameters)
    return sorted(used - declared)
