"""Base adapter interface for all tool integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class AdapterResult:
    """Standardised result from any tool adapter."""

    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    raw_output: str = ""
    error: str = ""
    evidence_snippet: str = ""
    # A failure that trying again cannot help: the request was refused, by a guard
    # or by the target. The engine does not retry it and hands the run to a person.
    escalate: bool = False
    # What was asked, for the step's audit record. Never evidence and never a credential.
    audit: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.success:
            return self.evidence_snippet or str(self.data)[:200]
        return f"ERROR: {self.error}"


class BaseAdapter(ABC):
    """Contract that every tool adapter must implement."""

    @abstractmethod
    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Run the adapter action and return a structured result."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the external tool is reachable."""

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Human-readable adapter name for logging."""
