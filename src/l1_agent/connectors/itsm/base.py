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


class ChangeUnavailable(RuntimeError):
    """A change record could not be opened or read. Without one, no write is made."""


@dataclass(frozen=True)
class ChangeRef:
    sys_id: str
    number: str


@dataclass(frozen=True)
class ChangeApproval:
    """Where an approval asked for on a change record stands.

    `state` is requested, approved or rejected. `correlation` is what the
    change record carries now, to be compared with the action's hash.
    """

    state: str
    approver: str = ""  # the approver's user name
    approver_email: str = ""
    comment: str = ""
    correlation: str = ""


class ChangeManagement(Protocol):
    """Change records for writes (ADR 0008). An ITSM without it cannot back an action."""

    async def open_change(
        self,
        template: str,
        *,
        correlation: str,
        short_description: str,
        description: str,
        incident_sys_id: str,
    ) -> ChangeRef:
        """Open a standard change from a named template, once per `correlation`."""

    async def request_change_approval(self, change: ChangeRef, group: str) -> int:
        """Ask the members of a group to approve. Returns how many were asked."""

    async def change_approval(self, change: ChangeRef) -> ChangeApproval: ...

    async def note_change(self, change: ChangeRef, note: str) -> None: ...

    async def close_change(self, change: ChangeRef, successful: bool, notes: str) -> None: ...


class Itsm(Protocol):
    async def get_new_incidents(self, assignment_group: str = "") -> List[Incident]: ...

    async def get_incident(self, sys_id: str) -> Incident: ...

    async def claim(self, incident: Incident) -> ClaimResult:
        """Take ownership before doing anything. Safe to call again for the same ticket."""

    async def add_work_note(self, sys_id: str, note: str) -> Dict[str, Any]: ...

    async def update_incident(self, sys_id: str, fields: Dict[str, Any]) -> Dict[str, Any]: ...

    async def resolve_incident(self, sys_id: str, close_notes: str) -> Dict[str, Any]: ...

    async def close(self) -> None: ...
