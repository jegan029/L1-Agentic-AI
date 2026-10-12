"""Connector manifests (ADR 0005).

A manifest says what a connector can do before any of its code runs: each
capability, whether it reads or writes, its risk tier, the closed schema of
its parameters and which of them are held to a tenant allow list. `lint`
is run by a test over every registered connector, so a connector that
declares a write without the things a write needs does not ship.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.l1_agent.engine.parameters import PLACEHOLDER_RE


class Family(str, Enum):
    ITSM = "itsm"
    KNOWLEDGE = "knowledge"
    OPERATIONS = "operations"


class Kind(str, Enum):
    READ = "read"
    WRITE = "write"


class RiskTier(str, Enum):
    """T3 is a policy classification for things that are never done; it cannot be declared."""

    T0 = "T0"  # read only
    T1 = "T1"  # low-risk, reversible write
    T2 = "T2"  # write that always needs an approval


READ_TIERS = frozenset({RiskTier.T0})
WRITE_TIERS = frozenset({RiskTier.T1, RiskTier.T2})

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_CONNECTOR_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


@dataclass(frozen=True)
class AllowListBinding:
    """A parameter that names a target, and the connection setting that lists what is allowed.

    `match` is how the SDK compares before the connector runs: `exact` for a
    whole-value match, `pattern` for shell-style patterns (`PAY_*`), `custom`
    when the connector does the detailed comparison itself (a path prefix, a
    URL host). In every case an empty list denies.
    """

    parameter: str
    config_key: str
    match: str = "custom"


ALLOW_LIST_MATCHES = ("exact", "pattern", "custom")


@dataclass(frozen=True)
class PreCheck:
    """A read made before a write, and what must be true of its result for the write to go on.

    The read is given those of the write's parameters that it declares, plus `parameters`.
    """

    capability: str
    assertion: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verify:
    """The read that shows a write worked.

    `assertion` true means verified. `failed` true means it has failed and
    there is no point waiting longer. Otherwise the read is repeated, up to
    `retries` more times, `wait_seconds` apart; still undecided is a failure.
    """

    capability: str
    assertion: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    failed: str = ""
    retries: int = 0
    wait_seconds: float = 0.0


@dataclass(frozen=True)
class Capability:
    id: str
    kind: Kind
    tier: Optional[RiskTier]
    summary: str
    parameters: Mapping[str, Any]  # JSON Schema, an object with additionalProperties false
    output: Mapping[str, Any] = field(default_factory=dict)
    scopes: Tuple[str, ...] = ()
    allow_lists: Tuple[AllowListBinding, ...] = ()
    # Parameters that identify what is acted on. Each needs an allow list binding on a write.
    target_parameters: Tuple[str, ...] = ()
    supports_dry_run: bool = False
    idempotent: bool = True
    retryable_errors: Tuple[str, ...] = ()
    pre_checks: Tuple[PreCheck, ...] = ()
    verify: Optional[Verify] = None
    rollback: str = ""  # a capability id, or "none: <reason>"
    timeout_seconds: int = 60

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "tier": self.tier.value if self.tier else None,
            "summary": self.summary,
            "parameters": dict(self.parameters),
            "output": dict(self.output),
            "scopes": list(self.scopes),
            "allow_lists": [
                {"parameter": b.parameter, "config_key": b.config_key, "match": b.match}
                for b in self.allow_lists
            ],
            "target_parameters": list(self.target_parameters),
            "supports_dry_run": self.supports_dry_run,
            "idempotent": self.idempotent,
            "retryable_errors": list(self.retryable_errors),
            "pre_checks": [
                {"capability": c.capability, "assertion": c.assertion} for c in self.pre_checks
            ],
            "verify": (
                {
                    "capability": self.verify.capability,
                    "assertion": self.verify.assertion,
                    "failed": self.verify.failed,
                    "retries": self.verify.retries,
                    "wait_seconds": self.verify.wait_seconds,
                }
                if self.verify
                else None
            ),
            "rollback": self.rollback,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class SecretSlot:
    name: str
    purpose: str
    required: bool = True


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    family: Family
    display_name: str
    config: Mapping[str, Any] = field(default_factory=dict)  # JSON Schema of non-secret settings
    secrets: Tuple[SecretSlot, ...] = ()
    capabilities: Tuple[Capability, ...] = ()
    # Not in the default image, or not verified enough to be relied on.
    preview: bool = False
    notes: str = ""

    def capability(self, capability_id: str) -> Optional[Capability]:
        return next((c for c in self.capabilities if c.id == capability_id), None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "family": self.family.value,
            "display_name": self.display_name,
            "config": dict(self.config),
            "secrets": [
                {"name": s.name, "purpose": s.purpose, "required": s.required} for s in self.secrets
            ],
            "capabilities": [c.to_dict() for c in self.capabilities],
            "preview": self.preview,
            "notes": self.notes,
        }


# ── Lint ──────────────────────────────────────────────────────────────


def _closed_object(schema: Any) -> bool:
    return (
        isinstance(schema, Mapping)
        and schema.get("type") == "object"
        and schema.get("additionalProperties") is False
        and isinstance(schema.get("properties"), Mapping)
    )


def lint_capability(cap: Capability, known: Optional[frozenset] = None) -> List[str]:
    """Problems with one capability. `known` is every capability id in the registry."""
    label = f"capability '{cap.id}'"
    problems: List[str] = []
    if not _ID_RE.match(cap.id):
        problems.append(f"{label}: id must look like 'connector.thing.verb'")
    if not cap.summary:
        problems.append(f"{label}: a summary is required")
    if not _closed_object(cap.parameters):
        problems.append(
            f"{label}: parameters must be a JSON Schema object with additionalProperties false"
        )
    else:
        properties = cap.parameters["properties"]
        for name in cap.parameters.get("required", []):
            if name not in properties:
                problems.append(f"{label}: required parameter '{name}' is not declared")
        for binding in cap.allow_lists:
            if binding.parameter not in properties:
                problems.append(
                    f"{label}: allow list binding names unknown parameter '{binding.parameter}'"
                )
            if binding.match not in ALLOW_LIST_MATCHES:
                problems.append(
                    f"{label}: allow list match must be one of {list(ALLOW_LIST_MATCHES)}"
                )
        for name in cap.target_parameters:
            if name not in properties:
                problems.append(f"{label}: target parameter '{name}' is not declared")
    if cap.timeout_seconds <= 0:
        problems.append(f"{label}: timeout must be positive")

    if cap.kind == Kind.READ:
        if cap.tier not in READ_TIERS:
            problems.append(f"{label}: a read capability is tier T0")
        return problems

    # Everything below is what a write must declare.
    if cap.tier is None:
        problems.append(f"{label}: a write capability needs a risk tier")
    elif cap.tier not in WRITE_TIERS:
        problems.append(f"{label}: a write capability is tier T1 or T2")
    if not cap.scopes:
        problems.append(f"{label}: a write capability must list the scopes it needs")
    if cap.verify is None or not cap.verify.capability or not cap.verify.assertion:
        problems.append(f"{label}: a write capability needs a verify operation and assertion")
    else:
        if known is not None and cap.verify.capability not in known:
            problems.append(f"{label}: verify names unknown capability '{cap.verify.capability}'")
        for text in filter(None, (cap.verify.assertion, cap.verify.failed)):
            problems += _assertion_problems(f"{label}: verify", text)
        if not 0 <= cap.verify.retries <= 60 or not 0 <= cap.verify.wait_seconds <= 300:
            problems.append(f"{label}: verify waits at most 60 times, 300 seconds apart")
    if not cap.rollback:
        problems.append(f"{label}: declare a rollback capability or 'none: <reason>'")
    elif not cap.rollback.startswith("none:") and known is not None and cap.rollback not in known:
        problems.append(f"{label}: rollback names unknown capability '{cap.rollback}'")
    if not cap.target_parameters:
        problems.append(f"{label}: a write capability must name its target parameters")
    bound = {b.parameter for b in cap.allow_lists}
    for name in cap.target_parameters:
        if name not in bound:
            problems.append(f"{label}: target parameter '{name}' has no allow list binding")
    for check in cap.pre_checks:
        if not isinstance(check, PreCheck):
            problems.append(f"{label}: a pre-check is a capability with an assertion")
            continue
        if known is not None and check.capability not in known:
            problems.append(f"{label}: pre-check names unknown capability '{check.capability}'")
        problems += _assertion_problems(f"{label}: pre-check", check.assertion)
    return problems


def _assertion_problems(label: str, text: str) -> List[str]:
    """An assertion in a manifest must parse and may only look at the read's result."""
    from src.l1_agent.engine import expressions

    if not text:
        return [f"{label} needs an assertion"]
    try:
        names = expressions.root_names(text)
    except expressions.ExpressionError as exc:
        return [f"{label} assertion does not parse: {exc}"]
    return [
        f"{label} assertion uses unknown name '{n}'" for n in names if n not in ("result", "params")
    ]


def lint(manifest: Manifest, known: Optional[frozenset] = None) -> List[str]:
    """Every problem with a manifest. An empty list means it may be registered."""
    problems: List[str] = []
    if not _CONNECTOR_ID_RE.match(manifest.id):
        problems.append(f"connector '{manifest.id}': id must be a lower-case identifier")
    if not manifest.version or not manifest.display_name:
        problems.append(f"connector '{manifest.id}': version and display name are required")
    if manifest.config and not _closed_object(manifest.config):
        problems.append(
            f"connector '{manifest.id}': config must be a JSON Schema object with "
            "additionalProperties false"
        )
    ids = [c.id for c in manifest.capabilities]
    for duplicate in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"connector '{manifest.id}': capability '{duplicate}' is declared twice")
    names = [s.name for s in manifest.secrets]
    for duplicate in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"connector '{manifest.id}': secret slot '{duplicate}' is declared twice")
    config_keys = set((manifest.config or {}).get("properties", {}))
    for cap in manifest.capabilities:
        if not cap.id.startswith(manifest.id + "."):
            problems.append(f"capability '{cap.id}': id must start with '{manifest.id}.'")
        for binding in cap.allow_lists:
            if binding.config_key not in config_keys:
                problems.append(
                    f"capability '{cap.id}': allow list '{binding.config_key}' is not a "
                    "connection setting"
                )
        problems += lint_capability(cap, known)
    return problems


# ── Parameter validation ──────────────────────────────────────────────


def validate_parameters(
    schema: Mapping[str, Any],
    parameters: Any,
    *,
    subject: str,
    allow_placeholders: bool = False,
) -> List[str]:
    """Check parameters against a capability's closed schema.

    Supports the subset of JSON Schema that manifests use: type, enum,
    required, maxLength, pattern (the whole value must match), minimum and maximum. `subject` names the step type
    or capability in messages. `allow_placeholders` is for static validation
    of an SOP, where a value may still be an unrendered ``${name}``.
    """
    if not isinstance(parameters, dict):
        return ["parameters must be an object"]
    properties: Mapping[str, Any] = schema.get("properties", {})
    problems: List[str] = []
    for key in parameters:
        if key not in properties:
            problems.append(f"parameter '{key}' is not allowed for {subject}")
    for key in schema.get("required", []):
        if parameters.get(key) in (None, ""):
            problems.append(f"parameter '{key}' is required for {subject}")
    for key, value in parameters.items():
        if key not in properties:
            continue
        if allow_placeholders and isinstance(value, str) and PLACEHOLDER_RE.search(value):
            continue
        spec = properties[key]
        expected = _JSON_TYPES.get(spec.get("type", ""), object)
        # bool is a subclass of int; do not accept True where a number is expected.
        if isinstance(value, bool) and expected is not bool:
            problems.append(f"parameter '{key}' has the wrong type")
        elif not isinstance(value, expected):
            problems.append(f"parameter '{key}' has the wrong type")
        elif "enum" in spec and value not in spec["enum"]:
            problems.append(f"parameter '{key}' must be one of {list(spec['enum'])}")
        elif isinstance(value, str) and len(value) > spec.get("maxLength", 10_000):
            problems.append(f"parameter '{key}' is too long")
        elif (
            isinstance(value, str)
            and "pattern" in spec
            and not re.fullmatch(spec["pattern"], value)
        ):
            problems.append(f"parameter '{key}' has characters that are not allowed")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                problems.append(f"parameter '{key}' is below the minimum")
            if "maximum" in spec and value > spec["maximum"]:
                problems.append(f"parameter '{key}' is above the maximum")
    return problems
