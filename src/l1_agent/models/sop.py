"""SOP / Runbook data models and step types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class StepType(Enum):
    SPLUNK_SEARCH = "SPLUNK_SEARCH"
    MQ_CHECK = "MQ_CHECK"
    FILE_CHECK = "FILE_CHECK"
    AUTOSYS_STATUS = "AUTOSYS_STATUS"
    DYNATRACE_VM_CHECK = "DYNATRACE_VM_CHECK"
    DYNATRACE_METRICS = "DYNATRACE_METRICS"
    WEB_UI_CHECK = "WEB_UI_CHECK"
    MAINFRAME_CHECK = "MAINFRAME_CHECK"
    DECISION = "DECISION"
    NOTE = "NOTE"


@dataclass
class SOPStep:
    """A single executable step within an SOP."""

    step_id: str = ""
    step_type: str = ""
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    expected_output: str = ""
    on_success: str = ""
    on_failure: str = ""
    requires_approval: bool = False
    timeout_seconds: int = 60

    @classmethod
    def from_dict(cls, data: dict) -> "SOPStep":
        return cls(
            step_id=data.get("step_id", ""),
            step_type=data.get("step_type", ""),
            description=data.get("description", ""),
            parameters=data.get("parameters", {}),
            expected_output=data.get("expected_output", ""),
            on_success=data.get("on_success", ""),
            on_failure=data.get("on_failure", ""),
            requires_approval=data.get("requires_approval", False),
            timeout_seconds=data.get("timeout_seconds", 60),
        )


@dataclass
class EscalationCriteria:
    """Defines when to escalate to L2."""

    max_retry_count: int = 2
    escalate_on_access_denied: bool = True
    escalate_on_ambiguous_result: bool = True
    escalate_on_write_action: bool = True
    custom_rules: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "EscalationCriteria":
        return cls(
            max_retry_count=data.get("max_retry_count", 2),
            escalate_on_access_denied=data.get("escalate_on_access_denied", True),
            escalate_on_ambiguous_result=data.get("escalate_on_ambiguous_result", True),
            escalate_on_write_action=data.get("escalate_on_write_action", True),
            custom_rules=data.get("custom_rules", []),
        )


@dataclass
class SOP:
    """Represents a Standard Operating Procedure / Runbook."""

    sop_id: str = ""
    title: str = ""
    applicable_services: List[str] = field(default_factory=list)
    applicable_categories: List[str] = field(default_factory=list)
    applicable_assignment_groups: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    steps: List[SOPStep] = field(default_factory=list)
    pre_checks: List[str] = field(default_factory=list)
    tools_required: List[str] = field(default_factory=list)
    escalation_criteria: EscalationCriteria = field(
        default_factory=EscalationCriteria
    )
    version: str = "1.0"

    @classmethod
    def from_dict(cls, data: dict) -> "SOP":
        return cls(
            sop_id=data.get("sop_id", ""),
            title=data.get("title", ""),
            applicable_services=data.get("applicable_services", []),
            applicable_categories=data.get("applicable_categories", []),
            applicable_assignment_groups=data.get("applicable_assignment_groups", []),
            keywords=data.get("keywords", []),
            steps=[SOPStep.from_dict(s) for s in data.get("steps", [])],
            pre_checks=data.get("pre_checks", []),
            tools_required=data.get("tools_required", []),
            escalation_criteria=EscalationCriteria.from_dict(
                data.get("escalation_criteria", {})
            ),
            version=data.get("version", "1.0"),
        )

    def to_dict(self) -> dict:
        return {
            "sop_id": self.sop_id,
            "title": self.title,
            "applicable_services": self.applicable_services,
            "applicable_categories": self.applicable_categories,
            "applicable_assignment_groups": self.applicable_assignment_groups,
            "keywords": self.keywords,
            "steps": [
                {
                    "step_id": s.step_id,
                    "step_type": s.step_type,
                    "description": s.description,
                    "parameters": s.parameters,
                    "expected_output": s.expected_output,
                    "on_success": s.on_success,
                    "on_failure": s.on_failure,
                    "requires_approval": s.requires_approval,
                    "timeout_seconds": s.timeout_seconds,
                }
                for s in self.steps
            ],
            "pre_checks": self.pre_checks,
            "tools_required": self.tools_required,
            "escalation_criteria": {
                "max_retry_count": self.escalation_criteria.max_retry_count,
                "escalate_on_access_denied": self.escalation_criteria.escalate_on_access_denied,
                "escalate_on_ambiguous_result": self.escalation_criteria.escalate_on_ambiguous_result,
                "escalate_on_write_action": self.escalation_criteria.escalate_on_write_action,
                "custom_rules": self.escalation_criteria.custom_rules,
            },
            "version": self.version,
        }
