"""Strict validation of a model's answer against the schema of the task that asked (ADR 0009).

The schemas the advisor uses are a small, closed subset of JSON Schema:
objects with listed properties, strings with a maximum length and optionally
an enum or a pattern, numbers and integers with bounds, booleans, and arrays
with a maximum number of items. A schema that uses anything else is refused,
and so is an answer with anything the schema does not list.
"""

from __future__ import annotations

import math
import re
from typing import Any, List, Mapping

MAX_STRING = 20_000
MAX_ITEMS = 100
_KNOWN = {
    "object": {"type", "properties", "required", "additionalProperties", "description"},
    "string": {"type", "maxLength", "enum", "pattern", "description"},
    "number": {"type", "minimum", "maximum", "description"},
    "integer": {"type", "minimum", "maximum", "description"},
    "boolean": {"type", "description"},
    "array": {"type", "items", "maxItems", "description"},
}


class SchemaError(ValueError):
    """The schema itself is outside the supported subset. A programming error."""


def check_schema(schema: Any, where: str = "$") -> None:
    """Refuse a schema this validator would not enforce completely."""
    if not isinstance(schema, Mapping) or schema.get("type") not in _KNOWN:
        raise SchemaError(f"{where}: unsupported or missing type")
    kind = schema["type"]
    extra = set(schema) - _KNOWN[kind]
    if extra:
        raise SchemaError(f"{where}: unsupported keywords {sorted(extra)}")
    if kind == "object":
        if schema.get("additionalProperties", True) is not False:
            raise SchemaError(f"{where}: an object must set additionalProperties to false")
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in properties:
                raise SchemaError(f"{where}: required property '{name}' is not declared")
        for name, child in properties.items():
            check_schema(child, f"{where}.{name}")
    elif kind == "string":
        if "enum" not in schema and "maxLength" not in schema:
            raise SchemaError(f"{where}: a string needs maxLength or enum")
    elif kind == "array":
        if "maxItems" not in schema or "items" not in schema:
            raise SchemaError(f"{where}: an array needs items and maxItems")
        check_schema(schema["items"], f"{where}[]")


def problems(schema: Mapping[str, Any], value: Any, where: str = "$") -> List[str]:
    """Why `value` does not fit `schema`. An empty list means it fits."""
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            return [f"{where}: not an object"]
        properties: Mapping[str, Any] = schema.get("properties", {})
        found = [f"{where}: '{k}' is not allowed" for k in value if k not in properties]
        found += [
            f"{where}: '{k}' is required" for k in schema.get("required", []) if k not in value
        ]
        for key, child in properties.items():
            if key in value:
                found += problems(child, value[key], f"{where}.{key}")
        return found
    if kind == "string":
        if not isinstance(value, str):
            return [f"{where}: not a string"]
        if "enum" in schema and value not in schema["enum"]:
            return [f"{where}: not one of the allowed values"]
        if len(value) > min(schema.get("maxLength", MAX_STRING), MAX_STRING):
            return [f"{where}: too long"]
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            return [f"{where}: does not match the pattern"]
        return []
    if kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{where}: not a number"]
        if kind == "integer" and not isinstance(value, int):
            return [f"{where}: not an integer"]
        if not math.isfinite(value):
            return [f"{where}: not a finite number"]
        if "minimum" in schema and value < schema["minimum"]:
            return [f"{where}: below the minimum"]
        if "maximum" in schema and value > schema["maximum"]:
            return [f"{where}: above the maximum"]
        return []
    if kind == "boolean":
        return [] if isinstance(value, bool) else [f"{where}: not a boolean"]
    if kind == "array":
        if not isinstance(value, list):
            return [f"{where}: not an array"]
        if len(value) > min(schema["maxItems"], MAX_ITEMS):
            return [f"{where}: too many items"]
        found = []
        for index, item in enumerate(value):
            found += problems(schema["items"], item, f"{where}[{index}]")
        return found
    return [f"{where}: unsupported type"]
