"""Static SOP validation.

An SOP that has any problem is not loaded and is never executed. The central
rule is the one in docs/adr/0002-fail-closed-outcomes.md: every route to a
RESOLVE step passes the success edge of a VERIFY step, with nothing but NOTE
steps in between.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from src.l1_agent.adapters.schemas import (
    find_capability,
    is_step_capability,
    validate_step_parameters,
)
from src.l1_agent.connectors.sdk.manifest import Kind
from src.l1_agent.engine import expressions
from src.l1_agent.engine.parameters import spec_problems, undeclared_placeholders
from src.l1_agent.models.sop import (
    CHECK_STEP_TYPES,
    SOP,
    TERMINAL_STEP_TYPES,
    SOPStep,
    StepType,
)

# The one pre-existing rule whose true branch is on_failure. Kept so that
# existing SOPs do not silently change direction.
LEGACY_ANY_FAILED_RULE = "any_failed"

DECISION_ROOTS = frozenset({"steps", "status", "incident", "params", "all_success", "any_failed"})
VERIFY_ROOTS = DECISION_ROOTS | {"result"}
MAX_VERIFY_RETRIES = 10
MAX_VERIFY_WAIT_SECONDS = 300
KNOWN_STEP_TYPES = frozenset(t.value for t in StepType)


class SOPValidationError(ValueError):
    """Raised by ``ensure_valid`` with every problem found."""

    def __init__(self, sop_id: str, problems: List[str]) -> None:
        self.sop_id = sop_id
        self.problems = problems
        super().__init__(f"SOP '{sop_id}' is not valid: " + "; ".join(problems))


def step_edges(step: SOPStep) -> List[Tuple[str, Optional[bool]]]:
    """Outgoing edges as (target step id, verified flag after taking the edge).

    A flag of None means the edge passes the incoming flag through unchanged.
    """
    step_type = step.step_type.upper()
    if step.requires_approval or step_type in TERMINAL_STEP_TYPES:
        return []
    edges: List[Tuple[str, Optional[bool]]] = []
    if step_type == StepType.VERIFY.value:
        if step.on_success:
            edges.append((step.on_success, True))
        if step.on_failure:
            edges.append((step.on_failure, False))
    elif step_type == StepType.NOTE.value:
        if step.on_success:
            edges.append((step.on_success, None))
        if step.on_failure:
            edges.append((step.on_failure, False))
    else:
        for target in (step.on_success, step.on_failure):
            if target:
                edges.append((target, False))
    return edges


def _expression_problems(label: str, text: object, roots: frozenset) -> List[str]:
    if not isinstance(text, str):
        return [f"{label}: expression must be a string"]
    try:
        used = expressions.root_names(text)
    except expressions.ExpressionError as exc:
        return [f"{label}: {exc}"]
    return [f"{label}: unknown name '{name}'" for name in used if name not in roots]


def _step_problems(step: SOPStep, index: Dict[str, SOPStep]) -> List[str]:
    problems: List[str] = []
    label = f"step '{step.step_id}'"
    step_type = step.step_type.upper()

    if step_type not in KNOWN_STEP_TYPES:
        return [f"{label}: unknown step type '{step.step_type}'"]
    if not isinstance(step.parameters, dict):
        return [f"{label}: parameters must be an object"]

    for edge_name, target in (("on_success", step.on_success), ("on_failure", step.on_failure)):
        if target and target not in index:
            problems.append(f"{label}: {edge_name} refers to unknown step '{target}'")

    if step_type in TERMINAL_STEP_TYPES:
        if step.on_success or step.on_failure:
            problems.append(f"{label}: a {step_type} step ends the run and cannot have edges")
        return problems

    if not step.on_success and not step.requires_approval:
        problems.append(f"{label}: on_success is required (end the run with ESCALATE or RESOLVE)")

    if step.capability and step_type != StepType.CHECK.value:
        problems.append(f"{label}: only a CHECK step names a capability")
    if step_type in CHECK_STEP_TYPES:
        problems += [
            f"{label}: {p}"
            for p in validate_step_parameters(step_type, step.parameters, allow_placeholders=True)
        ]
    elif step_type == StepType.CHECK.value:
        problems += _capability_problems(label, step.capability, step.parameters)
    elif step_type == StepType.DECISION.value:
        rule = step.parameters.get("rule", "")
        if rule != LEGACY_ANY_FAILED_RULE:
            problems += _expression_problems(f"{label} rule", rule, DECISION_ROOTS)
    elif step_type == StepType.VERIFY.value:
        extra = set(step.parameters) - {"check", "parameters", "assert", "retries", "wait_seconds"}
        if extra:
            problems.append(f"{label}: unknown VERIFY keys {sorted(extra)}")
        check = str(step.parameters.get("check", ""))
        if check.upper() in CHECK_STEP_TYPES:
            problems += [
                f"{label}: {p}"
                for p in validate_step_parameters(
                    check.upper(), step.parameters.get("parameters", {}), allow_placeholders=True
                )
            ]
        elif find_capability(check) is not None and check == check.lower():
            problems += _capability_problems(label, check, step.parameters.get("parameters", {}))
        else:
            problems.append(f"{label}: 'check' must be an adapter step type")
        problems += _expression_problems(
            f"{label} assert", step.parameters.get("assert"), VERIFY_ROOTS
        )
        retries = step.parameters.get("retries", 0)
        wait = step.parameters.get("wait_seconds", 0)
        if (
            isinstance(retries, bool)
            or not isinstance(retries, int)
            or not 0 <= retries <= MAX_VERIFY_RETRIES
        ):
            problems.append(f"{label}: retries must be between 0 and {MAX_VERIFY_RETRIES}")
        if (
            isinstance(wait, bool)
            or not isinstance(wait, (int, float))
            or not 0 <= wait <= MAX_VERIFY_WAIT_SECONDS
        ):
            problems.append(
                f"{label}: wait_seconds must be between 0 and {MAX_VERIFY_WAIT_SECONDS}"
            )
    return problems


def _capability_problems(label: str, capability_id: str, parameters: object) -> List[str]:
    """A check must name a read capability that exists, with parameters its schema accepts."""
    capability = find_capability(capability_id) if capability_id else None
    if capability is None or capability_id != capability.id:
        return [f"{label}: unknown capability '{capability_id}'"]
    if capability.kind is not Kind.READ:
        return [f"{label}: '{capability_id}' is a write and cannot be used as a check"]
    if not is_step_capability(capability_id):
        # An ITSM or knowledge connector's capability is not something a step can call.
        return [f"{label}: '{capability_id}' cannot be used in an SOP step"]
    return [
        f"{label}: {p}"
        for p in validate_step_parameters(capability_id, parameters, allow_placeholders=True)
    ]


def _has_cycle(start: str, index: Dict[str, SOPStep]) -> bool:
    visiting: Set[str] = set()
    done: Set[str] = set()

    def visit(step_id: str) -> bool:
        if step_id in done:
            return False
        if step_id in visiting:
            return True
        visiting.add(step_id)
        for target, _ in step_edges(index[step_id]):
            if target in index and visit(target):
                return True
        visiting.discard(step_id)
        done.add(step_id)
        return False

    return visit(start)


def verified_flags_on_entry(sop: SOP) -> Dict[str, Set[bool]]:
    """For each reachable step, the set of 'verified' flags it can be entered with.

    Exhaustive over every path: the graph is acyclic and the flag has two
    values, so propagating (step, flag) pairs to a fixed point visits them all.
    """
    index = {s.step_id: s for s in sop.steps}
    flags: Dict[str, Set[bool]] = {s.step_id: set() for s in sop.steps}
    if not sop.steps:
        return flags
    pending: List[Tuple[str, bool]] = [(sop.steps[0].step_id, False)]
    while pending:
        step_id, flag = pending.pop()
        if step_id not in index or flag in flags[step_id]:
            continue
        flags[step_id].add(flag)
        for target, edge_flag in step_edges(index[step_id]):
            pending.append((target, flag if edge_flag is None else edge_flag))
    return flags


def validate_sop(sop: SOP) -> List[str]:
    """Return every problem found. An empty list means the SOP may execute."""
    problems: List[str] = []
    if sop.is_draft:
        problems.append(f"status is '{sop.status}'; only a published SOP can execute")
    if not sop.sop_id:
        problems.append("sop_id is required")
    if not sop.steps:
        problems.append("an SOP needs at least one step")
        return problems

    ids = [s.step_id for s in sop.steps]
    if any(not i for i in ids):
        problems.append("every step needs a step_id")
    duplicates = sorted({i for i in ids if i and ids.count(i) > 1})
    if duplicates:
        problems.append(f"duplicate step ids: {duplicates}")
    if problems:
        return problems

    index = {s.step_id: s for s in sop.steps}

    names = [p.name for p in sop.parameters]
    dup_params = sorted({n for n in names if names.count(n) > 1})
    if dup_params:
        problems.append(f"duplicate parameter names: {dup_params}")
    for spec in sop.parameters:
        problems += spec_problems(spec)
    for name in undeclared_placeholders(sop):
        problems.append(f"placeholder '${{{name}}}' is used but no parameter declares it")

    for step in sop.steps:
        problems += _step_problems(step, index)
    if problems:
        return problems

    if _has_cycle(sop.steps[0].step_id, index):
        problems.append("the step graph contains a cycle")
        return problems

    flags = verified_flags_on_entry(sop)
    for step in sop.steps:
        if step.step_type.upper() == StepType.RESOLVE.value and False in flags[step.step_id]:
            problems.append(
                f"step '{step.step_id}': RESOLVE can be reached without a passed VERIFY step"
            )
    return problems


def ensure_valid(sop: SOP) -> None:
    problems = validate_sop(sop)
    if problems:
        raise SOPValidationError(sop.sop_id, problems)
