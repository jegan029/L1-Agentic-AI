"""No route to RESOLVED without a passed verification (ADR 0002).

Four layers, as the ADR lists them:
  1. search of the run state machine's transition table
  2. search of every sample SOP's step graph
  3. property-based execution of random SOP graphs with adversarial adapters
  4. a mutation guard, so the searches cannot silently become vacuous
Plus direct reproductions of the defects this replaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.adapters.mock_adapters import (
    MockAutosysAdapter,
    MockDynatraceAdapter,
    MockSplunkAdapter,
)
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.engine.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    RunEvent,
    RunState,
    transition,
)
from src.l1_agent.models.evidence import (
    ExecutionOutcome,
    ExecutionSummary,
    StepStatus,
    UnverifiedResolutionError,
)
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, SOPStep
from src.l1_agent.sop.validator import step_edges, validate_sop

SAMPLE_DIR = Path(__file__).resolve().parents[3] / "data" / "sample_sops"
SAMPLE_FILES = sorted(SAMPLE_DIR.glob("*.json"))


# ── 1. Transition table search ────────────────────────────────────────────


def _reachable(table: Dict[Tuple[RunState, RunEvent], RunState]) -> Set[Tuple[RunState, bool]]:
    """Every (state, verified) pair reachable from RECEIVED, over every path.

    ``verified`` is tracked from the events alone, independently of where the
    table sends them: it becomes true on a passed verification, survives notes
    and the resolve request, and is cleared by anything else. Exploring pairs
    to a fixed point covers all paths, including ones that loop.
    """
    start = (RunState.RECEIVED, False)
    seen: Set[Tuple[RunState, bool]] = {start}
    pending = [start]
    while pending:
        state, verified = pending.pop()
        for (src, event), dst in table.items():
            if src != state:
                continue
            if (src, event) == (RunState.VERIFYING, RunEvent.VERIFY_PASSED):
                after = True
            elif event in (RunEvent.NOTE_DONE, RunEvent.RESOLVE):
                after = verified
            else:
                after = False
            pair = (dst, after)
            if pair not in seen:
                seen.add(pair)
                if dst not in TERMINAL_STATES:
                    pending.append(pair)
    return seen


def _unverified_paths(table: Dict[Tuple[RunState, RunEvent], RunState]) -> bool:
    """True when some path reaches RESOLVED without a standing passed verification."""
    return (RunState.RESOLVED, False) in _reachable(table)


class TestTransitionTable:
    def test_resolved_is_reachable(self):
        """Guard against a vacuous pass: a verified resolution must be possible."""
        assert (RunState.RESOLVED, True) in _reachable(TRANSITIONS)

    def test_every_path_to_resolved_passes_verification(self):
        assert not _unverified_paths(TRANSITIONS)

    def test_search_covers_every_state(self):
        assert {state for state, _ in _reachable(TRANSITIONS)} == set(RunState)

    def test_only_one_edge_enters_resolved(self):
        entering = [k for k, dst in TRANSITIONS.items() if dst == RunState.RESOLVED]
        assert entering == [(RunState.VERIFIED, RunEvent.RESOLVE)]

    def test_only_a_passed_verification_enters_verified(self):
        entering = {k for k, dst in TRANSITIONS.items() if dst == RunState.VERIFIED}
        assert entering == {
            (RunState.VERIFYING, RunEvent.VERIFY_PASSED),
            (RunState.VERIFIED, RunEvent.NOTE_DONE),
        }

    def test_an_action_never_leaves_the_run_verified(self):
        """Every state an ACTION step passes through leads on to EXECUTING or to an end."""
        action_states = {
            RunState.POLICY_CHECK,
            RunState.AWAITING_APPROVAL,
            RunState.ACTING,
            RunState.ROLLING_BACK,
        }
        leaving = {dst for (src, _), dst in TRANSITIONS.items() if src in action_states}
        assert leaving == action_states - {RunState.POLICY_CHECK} | {
            RunState.EXECUTING,
            RunState.ESCALATED,
            RunState.NEEDS_HUMAN,
            RunState.ABORTED,
        }
        # A failed action is always rolled back or handed over; it never just continues.
        assert TRANSITIONS[(RunState.ACTING, RunEvent.ACTION_FAILED)] == RunState.ROLLING_BACK
        assert [d for (s, _), d in TRANSITIONS.items() if s == RunState.ROLLING_BACK] == [
            RunState.ESCALATED,
            RunState.NEEDS_HUMAN,
            RunState.NEEDS_HUMAN,
            RunState.ABORTED,
        ]

    def test_unknown_pair_fails_closed(self):
        for state in RunState:
            if state in TERMINAL_STATES:
                continue
            for event in RunEvent:
                if (state, event) not in TRANSITIONS:
                    assert transition(state, event) == RunState.NEEDS_HUMAN

    def test_terminal_states_have_no_exits(self):
        assert not [k for k in TRANSITIONS if k[0] in TERMINAL_STATES]

    def test_every_non_terminal_state_can_reach_a_human(self):
        for state in RunState:
            if state not in TERMINAL_STATES:
                assert transition(state, RunEvent.ERROR) == RunState.NEEDS_HUMAN
                assert transition(state, RunEvent.ABORT) == RunState.ABORTED


class TestMutationGuard:
    """If someone adds a shortcut to RESOLVED, the search must notice."""

    @pytest.mark.parametrize(
        "extra",
        [
            {(RunState.EXECUTING, RunEvent.RESOLVE): RunState.RESOLVED},
            {(RunState.EXECUTING, RunEvent.STEPS_EXHAUSTED): RunState.RESOLVED},
            {(RunState.BINDING, RunEvent.PARAMS_BOUND): RunState.VERIFIED},
            {(RunState.VERIFYING, RunEvent.VERIFY_FAILED): RunState.VERIFIED},
            {(RunState.VERIFIED, RunEvent.STEP_DONE): RunState.VERIFIED},
            # An action that worked is not a verification of the incident.
            {(RunState.ACTING, RunEvent.ACTION_VERIFIED): RunState.VERIFIED},
            {(RunState.POLICY_CHECK, RunEvent.POLICY_SHADOW): RunState.VERIFIED},
            {(RunState.ACTING, RunEvent.ACTION_NOT_DONE): RunState.VERIFIED},
            {(RunState.AWAITING_APPROVAL, RunEvent.APPROVAL_GRANTED): RunState.RESOLVED},
            {(RunState.ROLLING_BACK, RunEvent.ROLLBACK_DONE): RunState.RESOLVED},
            {(RunState.VERIFIED, RunEvent.ACTION_STARTED): RunState.VERIFIED},
        ],
    )
    def test_search_detects_a_shortcut(self, extra):
        mutated = {**TRANSITIONS, **extra}
        assert _unverified_paths(mutated), f"search missed mutation {extra}"


# ── 2. SOP graph search ───────────────────────────────────────────────────


def _resolve_paths(sop: SOP) -> List[List[Tuple[str, bool]]]:
    """Every path from the first step to a RESOLVE step, with the verified flag on entry."""
    index = {s.step_id: s for s in sop.steps}
    found: List[List[Tuple[str, bool]]] = []

    def walk(step_id: str, flag: bool, path: list) -> None:
        if step_id not in index or any(step_id == p[0] for p in path):
            return
        here = path + [(step_id, flag)]
        step = index[step_id]
        if step.step_type.upper() == "RESOLVE":
            found.append(here)
            return
        for target, edge_flag in step_edges(step):
            walk(target, flag if edge_flag is None else edge_flag, here)

    walk(sop.steps[0].step_id, False, [])
    return found


@pytest.mark.parametrize("path", SAMPLE_FILES, ids=lambda p: p.name)
class TestSampleSops:
    def test_sample_is_valid(self, path):
        sop = SOPParser().parse_from_json(json.loads(path.read_text(encoding="utf-8")))
        assert validate_sop(sop) == []

    def test_every_resolve_path_is_verified(self, path):
        sop = SOPParser().parse_from_json(json.loads(path.read_text(encoding="utf-8")))
        paths = _resolve_paths(sop)
        assert paths, "sample SOPs are expected to have a RESOLVE route"
        for p in paths:
            assert p[-1][1] is True, f"unverified route to RESOLVE: {[s for s, _ in p]}"


def test_there_are_sample_sops_to_check():
    assert len(SAMPLE_FILES) >= 3


# ── 3. Property-based: random graphs, adversarial adapters ────────────────


class ScriptedAdapter(BaseAdapter):
    """Returns pass, fail or raises, according to a script; then repeats the last entry."""

    adapter_name = "scripted"

    def __init__(self, script: List[str]) -> None:
        self._script = script or ["ok"]
        self._i = 0

    async def health_check(self) -> bool:
        return True

    async def execute(self, parameters):
        action = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if action == "raise":
            raise RuntimeError("boom")
        if action == "fail":
            return AdapterResult(success=False, error="scripted failure")
        if action == "ok_bad":
            return AdapterResult(success=True, data={"value": 0})
        if action == "ok_empty":
            return AdapterResult(success=True, data={})
        return AdapterResult(success=True, data={"value": 1})


STEP_IDS = ["a", "b", "c", "d", "e", "f"]
_target = st.sampled_from(STEP_IDS + ["", "", "missing"])


@st.composite
def random_step(draw, step_id: str) -> dict:
    step_type = draw(
        st.sampled_from(
            [
                "NOTE",
                "SPLUNK_SEARCH",
                "DECISION",
                "VERIFY",
                "RESOLVE",
                "ESCALATE",
                "ACTION",
                "BOGUS",
            ]
        )
    )
    step = {
        "step_id": step_id,
        "step_type": step_type,
        "on_success": draw(_target),
        "on_failure": draw(_target),
        "requires_approval": draw(st.sampled_from([False, False, False, True])),
    }
    if step_type == "SPLUNK_SEARCH":
        step["parameters"] = {"query": "index=main"}
    elif step_type == "ACTION":
        step["capability"] = draw(
            st.sampled_from(["autosys.job.force_start", "autosys.job.status", "nope.nope"])
        )
        step["parameters"] = {"job_name": "NIGHTLY_X"}
    elif step_type == "DECISION":
        step["parameters"] = {
            "rule": draw(
                st.sampled_from(
                    ["all_success", "any_failed", "not any_failed", "junk ==", "depth > 1"]
                )
            )
        }
    elif step_type == "VERIFY":
        step["parameters"] = {
            "check": "SPLUNK_SEARCH",
            "parameters": {"query": "index=main"},
            "assert": draw(
                st.sampled_from(["result.value == 1", "result.missing == 1", "result.value"])
            ),
        }
    else:
        step["parameters"] = {}
    return step


@st.composite
def random_sop(draw) -> SOP:
    count = draw(st.integers(min_value=0, max_value=len(STEP_IDS)))
    steps = [draw(random_step(STEP_IDS[i])) for i in range(count)]
    return SOP(
        sop_id="SOP-RND",
        title="random",
        steps=[SOPStep.from_dict(s) for s in steps],
        change_template=draw(st.sampled_from(["Standard restart", "Standard restart", ""])),
    )


def _verify_passed_before_resolve(summary: ExecutionSummary) -> bool:
    """Independent check on the run record: RESOLVE is immediately preceded by a
    passed VERIFY, allowing only notes in between."""
    results = summary.step_results
    if not results or results[-1].step_type.upper() != "RESOLVE":
        return False
    for r in reversed(results[:-1]):
        kind = r.step_type.upper()
        if kind == "NOTE":
            continue
        return (
            kind == "VERIFY" and r.status == StepStatus.SUCCESS and r.step_id == summary.verified_by
        )
    return False


class TestRandomGraphs:
    @settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        sop=random_sop(),
        script=st.lists(st.sampled_from(["ok", "ok_bad", "ok_empty", "fail", "raise"]), max_size=8),
    )
    @pytest.mark.asyncio
    async def test_resolved_always_has_a_passed_verification(self, sop, script):
        scheduler = MockAutosysAdapter()
        executor = SOPExecutor(
            adapters={"splunk": ScriptedAdapter(script), "autosys": scheduler},
            retry_max_attempts=1,
            retry_base_delay=0,
        )
        incident = Incident(sys_id="x", number="INC1", short_description="anything")
        valid = validate_sop(sop) == []

        summary = await executor.execute_sop(incident, sop)

        # An engine with no action pipeline never writes, whatever the SOP says.
        assert scheduler.force_starts == []

        assert summary.outcome is not None, "every run ends in a terminal outcome"
        if not valid:
            assert summary.outcome == ExecutionOutcome.NEEDS_HUMAN
            assert summary.step_results == [], "an invalid SOP must not start"
        if summary.outcome == ExecutionOutcome.RESOLVED:
            assert valid
            assert _verify_passed_before_resolve(summary)
        else:
            assert summary.verified_by == ""

    @settings(max_examples=300, deadline=None)
    @given(sop=random_sop())
    def test_validator_agrees_with_exhaustive_path_search(self, sop):
        """For structurally sound graphs, the validator's dataflow result matches
        a brute-force enumeration of every path to a RESOLVE step."""
        problems = validate_sop(sop)
        if any("RESOLVE can be reached" in p for p in problems):
            assert any(p[-1][1] is False for p in _resolve_paths(sop))
        elif not problems:
            assert all(p[-1][1] is True for p in _resolve_paths(sop))


# ── Reproductions of the defects this replaces ────────────────────────────


class FailingAdapter(BaseAdapter):
    adapter_name = "failing"

    async def health_check(self) -> bool:
        return False

    async def execute(self, parameters):
        return AdapterResult(success=False, error="boom")


def _incident(**kw) -> Incident:
    base = dict(sys_id="x", number="INC1", short_description="Autosys job PAYROLL_LOAD failed")
    base.update(kw)
    return Incident(**base)


class TestFormerDefects:
    @pytest.mark.asyncio
    async def test_sop_with_no_steps_does_not_resolve(self):
        summary = await SOPExecutor(adapters={}).execute_sop(
            _incident(), SOP(sop_id="EMPTY", title="empty")
        )
        assert summary.outcome == ExecutionOutcome.NEEDS_HUMAN
        assert "at least one step" in summary.escalation_reason

    @pytest.mark.asyncio
    async def test_failed_step_routed_to_a_note_does_not_resolve(self):
        """The old sample SOP shape: on_failure points at a NOTE and the SOP just ends."""
        sop = SOP.from_dict(
            {
                "sop_id": "OLD",
                "title": "old shape",
                "steps": [
                    {
                        "step_id": "s1",
                        "step_type": "SPLUNK_SEARCH",
                        "parameters": {"query": "x"},
                        "on_success": "s-ok",
                        "on_failure": "step-escalate",
                    },
                    {"step_id": "s-ok", "step_type": "NOTE"},
                    {"step_id": "step-escalate", "step_type": "NOTE"},
                ],
            }
        )
        summary = await SOPExecutor(adapters={"splunk": FailingAdapter()}).execute_sop(
            _incident(), sop
        )
        assert summary.outcome == ExecutionOutcome.NEEDS_HUMAN  # rejected before it starts

    @pytest.mark.asyncio
    async def test_failed_step_without_failure_edge_escalates(self):
        sop = SOP.from_dict(
            {
                "sop_id": "S",
                "title": "s",
                "steps": [
                    {
                        "step_id": "s1",
                        "step_type": "SPLUNK_SEARCH",
                        "parameters": {"query": "x"},
                        "on_success": "end",
                    },
                    {"step_id": "end", "step_type": "ESCALATE"},
                ],
            }
        )
        summary = await SOPExecutor(
            adapters={"splunk": FailingAdapter()}, retry_max_attempts=1, retry_base_delay=0
        ).execute_sop(_incident(), sop)
        assert summary.outcome == ExecutionOutcome.ESCALATED
        assert "s1 failed" in summary.escalation_reason

    @pytest.mark.asyncio
    async def test_unrecognised_decision_rule_does_not_take_the_success_branch(self):
        sop = SOP.from_dict(
            {
                "sop_id": "D",
                "title": "d",
                "steps": [
                    {
                        "step_id": "a",
                        "step_type": "DECISION",
                        "parameters": {"rule": "depth > 1000"},
                        "on_success": "ok",
                        "on_failure": "bad",
                    },
                    {"step_id": "ok", "step_type": "ESCALATE"},
                    {"step_id": "bad", "step_type": "ESCALATE"},
                ],
            }
        )
        summary = await SOPExecutor(adapters={}).execute_sop(_incident(), sop)
        assert summary.outcome == ExecutionOutcome.NEEDS_HUMAN
        assert summary.step_results == []

    @pytest.mark.asyncio
    async def test_decision_that_cannot_be_evaluated_needs_a_human(self):
        sop = SOP.from_dict(
            {
                "sop_id": "D2",
                "title": "d2",
                "steps": [
                    {
                        "step_id": "a",
                        "step_type": "DECISION",
                        "parameters": {"rule": 'steps["nope"].depth > 1000'},
                        "on_success": "ok",
                        "on_failure": "bad",
                    },
                    {"step_id": "ok", "step_type": "ESCALATE"},
                    {"step_id": "bad", "step_type": "ESCALATE"},
                ],
            }
        )
        summary = await SOPExecutor(adapters={}).execute_sop(_incident(), sop)
        assert summary.outcome == ExecutionOutcome.NEEDS_HUMAN
        assert summary.step_results[0].status == StepStatus.ERROR
        assert [r.step_id for r in summary.step_results] == ["a"]

    @pytest.mark.asyncio
    async def test_prose_knowledge_article_does_not_execute(self):
        kb = SOPParser().parse_from_servicenow(
            {
                "sys_id": "kb1",
                "short_description": "Restart the job",
                "text": "<p>Check the logs, then force start the job.</p>",
                "meta": "autosys,job",
            }
        )
        assert kb.is_draft
        summary = await SOPExecutor(adapters={}).execute_sop(_incident(), kb)
        assert summary.outcome == ExecutionOutcome.NEEDS_HUMAN

    @pytest.mark.asyncio
    async def test_memory_scenario_escalates_while_memory_is_still_high(self):
        sop = SOP.from_dict(json.loads((SAMPLE_DIR / "server_memory_high.json").read_text("utf-8")))
        executor = SOPExecutor(
            adapters={"dynatrace": MockDynatraceAdapter(), "splunk": MockSplunkAdapter()}
        )
        summary = await executor.execute_sop(
            _incident(short_description="High memory on app-server-prod01"), sop
        )
        assert summary.outcome == ExecutionOutcome.ESCALATED
        verify = next(r for r in summary.step_results if r.step_type == "VERIFY")
        assert verify.status == StepStatus.FAIL
        assert verify.data["latest"]["builtin:host.mem.usage"] == 92.5

    @pytest.mark.asyncio
    async def test_resolve_step_reached_unverified_is_refused_at_run_time(self):
        """Bypass the validator to prove the run-time guard holds on its own."""
        executor = SOPExecutor(adapters={})
        steps = [
            SOPStep(step_id="n", step_type="NOTE", on_success="r"),
            SOPStep(step_id="r", step_type="RESOLVE"),
        ]
        summary = ExecutionSummary(incident_number="INC1", sop_id="S", sop_title="s")
        state, reason, verified_by = await executor._run_steps(
            _incident(), steps, {}, summary, None
        )
        assert state == RunState.NEEDS_HUMAN
        assert verified_by == ""
        assert "without a passed verification" in reason


class TestSummaryGuard:
    def test_resolved_cannot_be_constructed_without_verification(self):
        with pytest.raises(UnverifiedResolutionError):
            ExecutionSummary(
                incident_number="I", sop_id="S", sop_title="s", outcome=ExecutionOutcome.RESOLVED
            )

    def test_resolved_cannot_be_assigned_without_verification(self):
        summary = ExecutionSummary(incident_number="I", sop_id="S", sop_title="s")
        with pytest.raises(UnverifiedResolutionError):
            summary.outcome = ExecutionOutcome.RESOLVED
        with pytest.raises(UnverifiedResolutionError):
            summary.conclude(ExecutionOutcome.RESOLVED)
        assert summary.outcome is None

    def test_verification_cannot_be_removed_from_a_resolved_summary(self):
        summary = ExecutionSummary(incident_number="I", sop_id="S", sop_title="s")
        summary.conclude(ExecutionOutcome.RESOLVED, verified_by="v1")
        with pytest.raises(UnverifiedResolutionError):
            summary.verified_by = ""

    def test_there_is_no_default_outcome(self):
        assert ExecutionSummary(incident_number="I", sop_id="S", sop_title="s").outcome is None

    def test_a_run_is_concluded_once(self):
        summary = ExecutionSummary(incident_number="I", sop_id="S", sop_title="s")
        summary.conclude(ExecutionOutcome.ESCALATED, reason="r")
        with pytest.raises(ValueError):
            summary.conclude(ExecutionOutcome.NEEDS_HUMAN)

    def test_outcome_vocabulary(self):
        assert {o.value for o in ExecutionOutcome} == {
            "resolved",
            "escalated",
            "needs_human",
            "aborted",
        }
