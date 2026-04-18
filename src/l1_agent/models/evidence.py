"""Evidence and step execution result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List


class StepStatus(Enum):
    SUCCESS = "success"
    FAIL = "fail"
    SKIP = "skip"
    ESCALATED = "escalated"


@dataclass
class StepResult:
    """Result of executing a single SOP step."""

    step_id: str
    step_type: str
    status: StepStatus
    evidence: str = ""
    error_message: str = ""
    tool_called: str = ""
    input_summary: str = ""
    output_summary: str = ""
    recommended_next_step: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    duration_ms: float = 0.0

    def to_audit_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "step_type": self.step_type,
            "status": self.status.value,
            "tool_called": self.tool_called,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "evidence_snippet": self.evidence[:500] if self.evidence else "",
            "error_message": self.error_message,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
        }


class ExecutionOutcome(Enum):
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class ExecutionSummary:
    """Summary of an entire SOP execution for an incident."""

    incident_number: str
    sop_id: str
    sop_title: str
    outcome: ExecutionOutcome
    step_results: List[StepResult] = field(default_factory=list)
    escalation_reason: str = ""
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    completed_at: str = ""
    total_duration_ms: float = 0.0

    def to_work_note(self) -> str:
        lines = [
            "[L1 Agent] SOP Execution Summary",
            f"SOP: {self.sop_title} ({self.sop_id})",
            f"Outcome: {self.outcome.value.upper()}",
            f"Steps executed: {len(self.step_results)}",
            "",
        ]
        for r in self.step_results:
            status_icon = {
                StepStatus.SUCCESS: "OK",
                StepStatus.FAIL: "FAIL",
                StepStatus.SKIP: "SKIP",
                StepStatus.ESCALATED: "ESC",
            }.get(r.status, "?")
            lines.append(f"  [{status_icon}] {r.step_id}: {r.output_summary}")
        if self.escalation_reason:
            lines.append(f"\nEscalation reason: {self.escalation_reason}")
        lines.append(f"\nDuration: {self.total_duration_ms:.0f}ms")
        return "\n".join(lines)

    def to_escalation_packet(self) -> dict:
        return {
            "incident_number": self.incident_number,
            "sop_id": self.sop_id,
            "sop_title": self.sop_title,
            "escalation_reason": self.escalation_reason,
            "checks_performed": [r.to_audit_dict() for r in self.step_results],
            "recommended_actions": "L2 review required",
        }
