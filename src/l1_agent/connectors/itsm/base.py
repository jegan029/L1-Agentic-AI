"""What the engine needs from an ITSM system (ADR 0005, ITSM family).

ServiceNow is the only implementation. The method names are the ones the
engine has always called; they will be renamed to neutral terms when a
second ITSM system is added and there is something to generalise from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Protocol

from src.l1_agent.models.incident import Incident


class ItsmUnavailable(RuntimeError):
    """The ITSM could not be reached or refused the agent. The run should be tried again."""


@dataclass(frozen=True)
class ClaimResult:
    """Whether the agent now owns the ticket. A ticket it does not own is left alone."""

    claimed: bool
    reason: str = ""


class Itsm(Protocol):
    async def get_new_incidents(self, assignment_group: str = "") -> List[Incident]: ...

    async def get_incident(self, sys_id: str) -> Incident: ...

    async def claim(self, incident: Incident) -> ClaimResult:
        """Take ownership before doing anything. Safe to call again for the same ticket."""

    async def add_work_note(self, sys_id: str, note: str) -> Dict[str, Any]: ...

    async def update_incident(self, sys_id: str, fields: Dict[str, Any]) -> Dict[str, Any]: ...

    async def resolve_incident(self, sys_id: str, close_notes: str) -> Dict[str, Any]: ...

    async def close(self) -> None: ...
