"""The durable record a run keeps so that it can be resumed.

The engine writes to a journal as it goes. When a worker dies and another
picks the run up, the journal says which SOP was chosen and which steps have
already completed, and the engine replays those results without calling the
target systems again. The database-backed journal is in service/runs.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from src.l1_agent.models.evidence import StepResult
from src.l1_agent.models.sop import SOP, SOPStep


@dataclass(frozen=True)
class StoredMatch:
    """The SOP decision made on the first attempt. A resumed run does not re-match."""

    sop_id: str
    sop_version: str
    confidence: float
    rationale: str
    # SHA-256 of the version's content, when it came from the version store.
    sop_hash: str = ""


class EventPublisher(Protocol):
    """Where the engine reports progress. The service's implementation writes to the database."""

    async def publish(self, event: dict) -> None: ...


class RunJournal(Protocol):
    @property
    def resumed(self) -> bool:
        """True when an earlier attempt already recorded progress."""

    @property
    def match(self) -> Optional[StoredMatch]:
        """The SOP chosen by an earlier attempt, if it got that far."""

    def completed(self, step_id: str) -> Optional[StepResult]:
        """The recorded result of a step that finished in an earlier attempt."""

    async def record_match(self, sop: SOP, confidence: float, rationale: str) -> None: ...

    async def step_started(self, step: SOPStep) -> None: ...

    async def step_completed(self, step: SOPStep, result: StepResult) -> None: ...
