"""Run-level state machine for incident processing.

The transition table is data so that tests can search it. Two rules hold by
construction and are checked by tests/unit/engine/test_no_unverified_resolve.py:

  * RESOLVED is entered only from VERIFIED, and VERIFIED only from VERIFYING
    on a passed assertion.
  * Any (state, event) pair that is not in the table goes to NEEDS_HUMAN.

See docs/adr/0002-fail-closed-outcomes.md.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Mapping, Tuple

from src.l1_agent.models.evidence import ExecutionOutcome


class RunState(str, Enum):
    RECEIVED = "received"
    MATCHING = "matching"
    BINDING = "binding"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    # Terminal
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    NEEDS_HUMAN = "needs_human"
    ABORTED = "aborted"


class RunEvent(str, Enum):
    START = "start"
    SOP_MATCHED = "sop_matched"
    NO_SOP = "no_sop"
    LOW_CONFIDENCE = "low_confidence"
    SOP_INVALID = "sop_invalid"
    PARAMS_BOUND = "params_bound"
    PARAMS_INVALID = "params_invalid"
    NOTE_DONE = "note_done"
    STEP_DONE = "step_done"  # a check or decision completed, pass or fail
    VERIFY_STARTED = "verify_started"
    VERIFY_PASSED = "verify_passed"
    VERIFY_FAILED = "verify_failed"
    ESCALATE = "escalate"
    RESOLVE = "resolve"
    STEPS_EXHAUSTED = "steps_exhausted"
    ERROR = "error"
    ABORT = "abort"


TERMINAL_STATES = frozenset(
    {RunState.RESOLVED, RunState.ESCALATED, RunState.NEEDS_HUMAN, RunState.ABORTED}
)

_S, _E = RunState, RunEvent

TRANSITIONS: Dict[Tuple[RunState, RunEvent], RunState] = {
    (_S.RECEIVED, _E.START): _S.MATCHING,
    (_S.MATCHING, _E.SOP_MATCHED): _S.BINDING,
    (_S.MATCHING, _E.NO_SOP): _S.ESCALATED,
    (_S.MATCHING, _E.LOW_CONFIDENCE): _S.ESCALATED,
    (_S.BINDING, _E.SOP_INVALID): _S.NEEDS_HUMAN,
    (_S.BINDING, _E.PARAMS_INVALID): _S.NEEDS_HUMAN,
    (_S.BINDING, _E.PARAMS_BOUND): _S.EXECUTING,
    (_S.EXECUTING, _E.NOTE_DONE): _S.EXECUTING,
    (_S.EXECUTING, _E.STEP_DONE): _S.EXECUTING,
    (_S.EXECUTING, _E.VERIFY_STARTED): _S.VERIFYING,
    (_S.EXECUTING, _E.ESCALATE): _S.ESCALATED,
    (_S.VERIFYING, _E.VERIFY_PASSED): _S.VERIFIED,
    (_S.VERIFYING, _E.VERIFY_FAILED): _S.EXECUTING,
    # After a passed verification only notes keep the run verified. Any other
    # work drops back to EXECUTING and needs a fresh VERIFY before RESOLVE.
    (_S.VERIFIED, _E.NOTE_DONE): _S.VERIFIED,
    (_S.VERIFIED, _E.STEP_DONE): _S.EXECUTING,
    (_S.VERIFIED, _E.VERIFY_STARTED): _S.VERIFYING,
    (_S.VERIFIED, _E.ESCALATE): _S.ESCALATED,
    (_S.VERIFIED, _E.RESOLVE): _S.RESOLVED,
}
# Every non-terminal state can be stopped or can fail into a human's hands.
for _state in RunState:
    if _state not in TERMINAL_STATES:
        TRANSITIONS[(_state, _E.ERROR)] = _S.NEEDS_HUMAN
        TRANSITIONS[(_state, _E.STEPS_EXHAUSTED)] = _S.NEEDS_HUMAN
        TRANSITIONS[(_state, _E.ABORT)] = _S.ABORTED

_OUTCOMES = {
    RunState.RESOLVED: ExecutionOutcome.RESOLVED,
    RunState.ESCALATED: ExecutionOutcome.ESCALATED,
    RunState.NEEDS_HUMAN: ExecutionOutcome.NEEDS_HUMAN,
    RunState.ABORTED: ExecutionOutcome.ABORTED,
}


class InvalidTransition(RuntimeError):
    """Raised when an event is applied to a run that has already ended."""


def transition(
    state: RunState,
    event: RunEvent,
    table: Mapping[Tuple[RunState, RunEvent], RunState] = TRANSITIONS,
) -> RunState:
    """Return the next state. Unknown pairs fail closed to NEEDS_HUMAN."""
    if state in TERMINAL_STATES:
        raise InvalidTransition(f"Run already ended in {state.value}; cannot apply {event.value}")
    return table.get((state, event), RunState.NEEDS_HUMAN)


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES


def outcome_of(state: RunState) -> ExecutionOutcome:
    """Map a terminal state to its outcome. Raises for a non-terminal state."""
    return _OUTCOMES[state]
