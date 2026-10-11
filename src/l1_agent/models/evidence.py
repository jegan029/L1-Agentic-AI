"""Evidence and step execution result models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class StepStatus(Enum):
    SUCCESS = "success"
    FAIL = "fail"
    SKIP = "skip"
    ESCALATED = "escalated"
    # The engine could not run the step safely (bad parameters, no adapter,
    # unknown step type or rule). Always ends the run as NEEDS_HUMAN.
    ERROR = "error"


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
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_ms: float = 0.0
    # Structured adapter output, used by DECISION and VERIFY expressions.
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Everything, for the run journal. `from_dict` restores an equal result."""
        return {**asdict(self), "status": self.status.value}

    @classmethod
    def from_dict(cls, data: dict) -> "StepResult":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        values["status"] = StepStatus(values["status"])
        return cls(**values)

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
    """Terminal states of a run. There is no default and no FAILED (ADR 0002)."""

    RESOLVED = "resolved"
    ESCALATED = "escalated"
    NEEDS_HUMAN = "needs_human"
    ABORTED = "aborted"


class UnverifiedResolutionError(ValueError):
    """Raised when something tries to record RESOLVED without a passed verification."""


@dataclass
class ExecutionSummary:
    """Summary of an entire SOP execution for an incident.

    ``outcome`` is None until the run reaches a terminal state. RESOLVED can
    only be recorded together with ``verified_by``, the id of the VERIFY step
    whose assertion passed.
    """

    incident_number: str
    sop_id: str
    sop_title: str
    outcome: Optional[ExecutionOutcome] = None
    step_results: List[StepResult] = field(default_factory=list)
    escalation_reason: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str = ""
    total_duration_ms: float = 0.0
    verified_by: str = ""

    def __post_init__(self) -> None:
        self._check_outcome(self.outcome, self.verified_by)
        object.__setattr__(self, "_initialised", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_initialised", False):
            if name == "outcome":
                self._check_outcome(value, self.verified_by)
            elif name == "verified_by":
                self._check_outcome(self.outcome, value)
        object.__setattr__(self, name, value)

    @staticmethod
    def _check_outcome(outcome: Optional[ExecutionOutcome], verified_by: str) -> None:
        if outcome is not None and not isinstance(outcome, ExecutionOutcome):
            raise TypeError(f"outcome must be an ExecutionOutcome or None, got {outcome!r}")
        if outcome == ExecutionOutcome.RESOLVED and not verified_by:
            raise UnverifiedResolutionError(
                "RESOLVED requires verified_by: the id of a VERIFY step that passed"
            )

    def conclude(
        self,
        outcome: ExecutionOutcome,
        *,
        reason: str = "",
        verified_by: str = "",
    ) -> None:
        """Record the terminal outcome. A summary can be concluded once."""
        if self.outcome is not None:
            raise ValueError(f"Run already concluded as {self.outcome.value}")
        self._check_outcome(outcome, verified_by)
        object.__setattr__(self, "verified_by", verified_by)
        object.__setattr__(self, "outcome", outcome)
        if reason:
            self.escalation_reason = reason

    @property
    def outcome_label(self) -> str:
        return self.outcome.value if self.outcome else "in_progress"

    def to_work_note(self) -> str:
        lines = [
            "[L1 Agent] SOP Execution Summary",
            f"SOP: {self.sop_title} ({self.sop_id})",
            f"Outcome: {self.outcome_label.upper()}",
            f"Steps executed: {len(self.step_results)}",
            "",
        ]
        for r in self.step_results:
            status_icon = {
                StepStatus.SUCCESS: "OK",
                StepStatus.FAIL: "FAIL",
                StepStatus.SKIP: "SKIP",
                StepStatus.ESCALATED: "ESC",
                StepStatus.ERROR: "ERR",
            }.get(r.status, "?")
            lines.append(f"  [{status_icon}] {r.step_id}: {r.output_summary}")
        if self.verified_by:
            lines.append(f"\nVerified by step: {self.verified_by}")
        if self.escalation_reason:
            lines.append(f"\nReason: {self.escalation_reason}")
        lines.append(f"\nDuration: {self.total_duration_ms:.0f}ms")
        return "\n".join(lines)

    def to_escalation_packet(self) -> dict:
        return {
            "incident_number": self.incident_number,
            "sop_id": self.sop_id,
            "sop_title": self.sop_title,
            "outcome": self.outcome_label,
            "escalation_reason": self.escalation_reason,
            "checks_performed": [r.to_audit_dict() for r in self.step_results],
            "recommended_actions": "L2 review required",
        }
