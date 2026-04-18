"""ServiceNow incident data model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IncidentState(Enum):
    NEW = 1
    IN_PROGRESS = 2
    ON_HOLD = 3
    RESOLVED = 6
    CLOSED = 7


class IncidentPriority(Enum):
    CRITICAL = 1
    HIGH = 2
    MODERATE = 3
    LOW = 4
    PLANNING = 5


@dataclass
class Incident:
    """Represents a ServiceNow incident record."""

    sys_id: str = ""
    number: str = ""
    short_description: str = ""
    description: str = ""
    category: str = ""
    subcategory: str = ""
    cmdb_ci: str = ""
    assignment_group: str = ""
    priority: int = 4
    state: int = 1
    caller: str = ""
    created_on: str = ""
    correlation_id: str = ""
    work_notes: str = ""

    def __post_init__(self) -> None:
        if not self.correlation_id:
            self.correlation_id = self.number

    @classmethod
    def from_servicenow(cls, data: dict) -> "Incident":
        """Create an Incident from a ServiceNow API response dict."""
        return cls(
            sys_id=data.get("sys_id", ""),
            number=data.get("number", ""),
            short_description=data.get("short_description", ""),
            description=data.get("description", ""),
            category=data.get("category", ""),
            subcategory=data.get("subcategory", ""),
            cmdb_ci=_display_value(data.get("cmdb_ci", "")),
            assignment_group=_display_value(data.get("assignment_group", "")),
            priority=_safe_int(data.get("priority", 4), 4),
            state=_safe_int(data.get("state", 1), 1),
            caller=_display_value(data.get("caller_id", "")),
            created_on=data.get("sys_created_on", ""),
        )

    def to_dict(self) -> dict:
        return {
            "sys_id": self.sys_id,
            "number": self.number,
            "short_description": self.short_description,
            "description": self.description,
            "category": self.category,
            "subcategory": self.subcategory,
            "cmdb_ci": self.cmdb_ci,
            "assignment_group": self.assignment_group,
            "priority": self.priority,
            "state": self.state,
            "caller": self.caller,
            "created_on": self.created_on,
        }


def _display_value(val: object) -> str:
    """Extract display_value from a ServiceNow reference field."""
    if isinstance(val, dict):
        return str(val.get("display_value", val.get("value", "")))
    return str(val)


def _safe_int(val: object, default: int) -> int:
    """Safely convert a value to int, handling ServiceNow dict format.

    When ``sysparm_display_value=all`` is used, choice/integer fields
    are returned as ``{"display_value": "2 - High", "value": "2"}``.
    """
    if isinstance(val, dict):
        val = val.get("value", default)
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
