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
    # A read check that names a connector capability in `capability`.
    CHECK = "CHECK"
    DECISION = "DECISION"
    NOTE = "NOTE"
    # A write: names a write capability in `capability`. Goes through policy (ADR 0008).
    ACTION = "ACTION"
    # Runs a read check and applies an assertion. The only route to RESOLVE.
    VERIFY = "VERIFY"
    # Explicit terminal steps.
    ESCALATE = "ESCALATE"
    RESOLVE = "RESOLVE"


# Step types that call a tool adapter.
CHECK_STEP_TYPES = frozenset(
    {
        StepType.SPLUNK_SEARCH.value,
        StepType.MQ_CHECK.value,
        StepType.FILE_CHECK.value,
        StepType.AUTOSYS_STATUS.value,
        StepType.DYNATRACE_VM_CHECK.value,
        StepType.DYNATRACE_METRICS.value,
        StepType.WEB_UI_CHECK.value,
        StepType.MAINFRAME_CHECK.value,
    }
)
# Every step type that calls a connector: the typed ones above and the generic CHECK.
CONNECTOR_STEP_TYPES = CHECK_STEP_TYPES | {StepType.CHECK.value}
TERMINAL_STEP_TYPES = frozenset({StepType.ESCALATE.value, StepType.RESOLVE.value})

SOP_STATUS_PUBLISHED = "published"
SOP_STATUS_DRAFT = "draft"


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
    # For a CHECK or ACTION step: the connector capability id, for example "splunk.search".
    capability: str = ""
    # For an ACTION step: overrides for the capability's own verification
    # ({"assert": ..., "retries": ..., "wait_seconds": ...}). Empty uses the manifest's.
    verify: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {
            "step_id": self.step_id,
            "step_type": self.step_type,
            "description": self.description,
            "parameters": self.parameters,
            "expected_output": self.expected_output,
            "on_success": self.on_success,
            "on_failure": self.on_failure,
            "requires_approval": self.requires_approval,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.capability:
            data["capability"] = self.capability
        if self.verify:
            data["verify"] = self.verify
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SOPStep":
        return cls(
            capability=data.get("capability", ""),
            verify=data.get("verify") or {},
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
class ParameterSpec:
    """A typed SOP parameter, bound from the ticket before any step runs.

    ``source`` is one of:
      {"kind": "ticket_field", "field": "<incident field>"}
      {"kind": "extraction", "fields": [...], "regex": "<pattern with one group>"}
      {"kind": "constant", "value": ...}
    A string parameter without ``pattern`` is held to a conservative default.
    """

    name: str = ""
    type: str = "string"
    source: Dict[str, Any] = field(default_factory=dict)
    pattern: str = ""
    enum: List[Any] = field(default_factory=list)
    max_length: int = 128
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ParameterSpec":
        return cls(
            name=data.get("name", ""),
            type=data.get("type", "string"),
            source=data.get("source", {}),
            pattern=data.get("pattern", ""),
            enum=data.get("enum", []),
            max_length=data.get("max_length", 128),
            description=data.get("description", ""),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "source": self.source,
            "pattern": self.pattern,
            "enum": self.enum,
            "max_length": self.max_length,
            "description": self.description,
        }


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
    escalation_criteria: EscalationCriteria = field(default_factory=EscalationCriteria)
    version: str = "1.0"
    parameters: List[ParameterSpec] = field(default_factory=list)
    # A draft is never matched and never executed.
    status: str = SOP_STATUS_PUBLISHED
    # SHA-256 of the published version's canonical content. Empty for an SOP that
    # did not come from the version store (tests, files).
    content_hash: str = ""
    # The standard change template every ACTION step of this SOP is carried out under.
    change_template: str = ""

    @property
    def is_draft(self) -> bool:
        return self.status != SOP_STATUS_PUBLISHED

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
            escalation_criteria=EscalationCriteria.from_dict(data.get("escalation_criteria", {})),
            version=data.get("version", "1.0"),
            parameters=[ParameterSpec.from_dict(p) for p in data.get("parameters", [])],
            status=data.get("status", SOP_STATUS_PUBLISHED),
            change_template=data.get("change_template", "") or "",
        )

    def to_dict(self) -> dict:
        data = {
            "sop_id": self.sop_id,
            "title": self.title,
            "status": self.status,
            "applicable_services": self.applicable_services,
            "applicable_categories": self.applicable_categories,
            "applicable_assignment_groups": self.applicable_assignment_groups,
            "keywords": self.keywords,
            "parameters": [p.to_dict() for p in self.parameters],
            "steps": [s.to_dict() for s in self.steps],
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
        # Written only when set, so that the hash of an SOP without actions does not change.
        if self.change_template:
            data["change_template"] = self.change_template
        return data
