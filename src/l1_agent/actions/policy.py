"""The policy decision for a write (ADR 0008).

`evaluate` is a pure function: the same request and the same situation give
the same ruling. The checks run in a fixed order and the first refusal wins.
Nothing here reads a model's output, and nothing here has a side effect.

The rules are data a tenant administrator owns (`policies.rules`):

    {
      "sop_autonomy": {"SOP-X": {"level": "automatic", "content_hash": "<sha256>"}},
      "raise_tier":   [{"capability": "autosys.job.force_start", "targets": ["PAY_*"]}],
      "automatic":    [{"sop_id": "SOP-X", "capability": "...", "targets": ["NIGHTLY_*"]}],
      "freeze":       [{"start": "2026-12-20T00:00:00Z", "end": "...", "reason": "year end"}],
      "hours":        {"days": [0, 1, 2, 3, 4], "start": "06:00", "end": "22:00"},
      "rate":         {"max_actions": 10, "per_minutes": 60},
      "approval":     {"ttl_minutes": 60, "execute_within_minutes": 15,
                       "channels": ["ui"], "servicenow_group": ""}
    }

Times are UTC. `days` counts Monday as 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.l1_agent.connectors.sdk.manifest import RiskTier


class Autonomy(str, Enum):
    SHADOW = "shadow"
    RECOMMEND = "recommend"
    APPROVAL = "approval"
    AUTOMATIC = "automatic"

    @property
    def rank(self) -> int:
        return _AUTONOMY_ORDER.index(self)


_AUTONOMY_ORDER = [Autonomy.SHADOW, Autonomy.RECOMMEND, Autonomy.APPROVAL, Autonomy.AUTOMATIC]


class Verdict(str, Enum):
    DENY = "deny"
    SHADOW = "shadow"
    RECOMMEND = "recommend"
    REQUIRE_APPROVAL = "require_approval"
    ALLOW = "allow"


# Reason codes. Each is the name of the check that decided.
KILL_SWITCH = "kill_switch"
NOT_A_WRITE = "not_a_declared_write"
ABOVE_CEILING = "above_deployment_ceiling"
CHANGE_FREEZE = "change_freeze"
OUTSIDE_HOURS = "outside_permitted_hours"
NOT_ON_ALLOW_LIST = "not_on_allow_list"
RATE_LIMIT = "rate_limit"
AUTONOMY = "autonomy"
TIER_NEEDS_APPROVAL = "tier_needs_approval"
NOT_ON_AUTOMATIC_LIST = "not_on_automatic_list"
ON_AUTOMATIC_LIST = "on_automatic_list"

CHANNEL_UI = "ui"
CHANNEL_SERVICENOW = "servicenow"
CHANNELS = (CHANNEL_UI, CHANNEL_SERVICENOW)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TOP_LEVEL = {"sop_autonomy", "raise_tier", "automatic", "freeze", "hours", "rate", "approval"}
MAX_RULES = 200


class PolicyError(ValueError):
    """The rules are not valid. Carries every problem found."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class TargetRule:
    capability: str
    targets: Tuple[str, ...]
    sop_id: str = ""

    def matches(self, capability_id: str, targets: Tuple[str, ...], sop_id: str = "") -> bool:
        """True when the capability matches and every target is covered by a pattern."""
        if self.capability != capability_id or (self.sop_id and self.sop_id != sop_id):
            return False
        return bool(targets) and all(
            any(fnmatchcase(t, pattern) for pattern in self.targets) for t in targets
        )

    def touches(self, capability_id: str, targets: Tuple[str, ...]) -> bool:
        """True when the capability matches and any target is covered. Used to raise a tier."""
        return self.capability == capability_id and any(
            fnmatchcase(t, pattern) for t in targets for pattern in self.targets
        )


@dataclass(frozen=True)
class Freeze:
    start: datetime
    end: datetime
    reason: str = ""


@dataclass(frozen=True)
class Hours:
    days: Tuple[int, ...]
    start: int  # minutes after midnight, UTC
    end: int

    def permits(self, now: datetime) -> bool:
        now = now.astimezone(timezone.utc)
        if now.weekday() not in self.days:
            return False
        minute = now.hour * 60 + now.minute
        if self.start <= self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end  # a window that crosses midnight


@dataclass(frozen=True)
class Rules:
    sop_autonomy: Mapping[str, Tuple[Autonomy, str]] = field(default_factory=dict)
    raise_tier: Tuple[TargetRule, ...] = ()
    automatic: Tuple[TargetRule, ...] = ()
    freeze: Tuple[Freeze, ...] = ()
    hours: Optional[Hours] = None
    rate_max: int = 0  # 0: no limit
    rate_minutes: int = 60
    approval_ttl_minutes: int = 60
    execute_within_minutes: int = 15
    channels: Tuple[str, ...] = (CHANNEL_UI,)
    servicenow_group: str = ""

    def sop_level(self, sop_id: str, content_hash: str) -> Autonomy:
        """The level set for this exact SOP version. Any other version is in shadow."""
        level, for_hash = self.sop_autonomy.get(sop_id, (Autonomy.SHADOW, ""))
        return level if for_hash and for_hash == content_hash else Autonomy.SHADOW


def _minutes(text: Any) -> Optional[int]:
    found = _TIME_RE.match(text) if isinstance(text, str) else None
    return int(found.group(1)) * 60 + int(found.group(2)) if found else None


def _when(text: Any) -> Optional[datetime]:
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _strings(value: Any) -> Optional[Tuple[str, ...]]:
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(v, str) or not v or len(v) > 200 for v in value):
        return None
    return tuple(value)


def _target_rules(name: str, value: Any, problems: List[str], with_sop: bool) -> Tuple:
    if not isinstance(value, list) or len(value) > MAX_RULES:
        problems.append(f"{name} must be a list of at most {MAX_RULES} rules")
        return ()
    found = []
    for i, item in enumerate(value):
        label = f"{name}[{i}]"
        allowed = {"capability", "targets"} | ({"sop_id"} if with_sop else set())
        if not isinstance(item, dict) or set(item) - allowed:
            problems.append(f"{label} may hold only {sorted(allowed)}")
            continue
        targets = _strings(item.get("targets"))
        capability = item.get("capability")
        sop_id = item.get("sop_id", "")
        if not isinstance(capability, str) or not capability:
            problems.append(f"{label}.capability is required")
        elif targets is None:
            problems.append(f"{label}.targets must be a non-empty list of names or patterns")
        elif with_sop and (not isinstance(sop_id, str) or not sop_id):
            # An automatic rule names the SOP: a capability is never automatic on its own.
            problems.append(f"{label}.sop_id is required")
        else:
            found.append(TargetRule(capability, targets, sop_id if with_sop else ""))
    return tuple(found)


def parse_rules(data: Any) -> Rules:
    """Read rules from their stored form. Raises PolicyError naming every problem."""
    if data in (None, {}):
        return Rules()
    if not isinstance(data, dict):
        raise PolicyError(["rules must be an object"])
    problems: List[str] = [f"unknown rule '{k}'" for k in sorted(set(data) - _TOP_LEVEL)]

    sop_autonomy: Dict[str, Tuple[Autonomy, str]] = {}
    raw = data.get("sop_autonomy", {})
    if not isinstance(raw, dict) or len(raw) > MAX_RULES:
        problems.append("sop_autonomy must be an object")
        raw = {}
    for sop_id, entry in raw.items():
        ok = isinstance(entry, dict) and set(entry) <= {"level", "content_hash"}
        level = entry.get("level") if ok else None
        digest = entry.get("content_hash", "") if ok else ""
        if level not in {a.value for a in Autonomy} or not _HASH_RE.match(str(digest)):
            problems.append(
                f"sop_autonomy['{sop_id}'] needs a level and the content_hash of the version"
            )
        else:
            sop_autonomy[str(sop_id)] = (Autonomy(level), digest)

    raise_tier = _target_rules("raise_tier", data.get("raise_tier", []), problems, False)
    automatic = _target_rules("automatic", data.get("automatic", []), problems, True)

    freeze: List[Freeze] = []
    windows = data.get("freeze", [])
    if not isinstance(windows, list) or len(windows) > MAX_RULES:
        problems.append("freeze must be a list")
        windows = []
    for i, item in enumerate(windows):
        ok = isinstance(item, dict) and set(item) <= {"start", "end", "reason"}
        start = _when(item.get("start")) if ok else None
        end = _when(item.get("end")) if ok else None
        if start is None or end is None or end <= start:
            problems.append(f"freeze[{i}] needs start and end as ISO times with an offset")
        else:
            freeze.append(Freeze(start, end, str(item.get("reason", ""))[:200]))

    hours: Optional[Hours] = None
    if "hours" in data:
        item = data["hours"]
        ok = isinstance(item, dict) and set(item) <= {"days", "start", "end"}
        days = item.get("days", list(range(7))) if ok else None
        start = _minutes(item.get("start")) if ok else None
        end = _minutes(item.get("end")) if ok else None
        days_ok = (
            isinstance(days, list)
            and days
            and all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days)
        )
        if not days_ok or start is None or end is None or start == end:
            problems.append("hours needs days (0 to 6, Monday is 0) and start and end as HH:MM")
        else:
            hours = Hours(tuple(sorted(set(days))), start, end)

    def whole(section: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            problems.append(f"{key} must be a whole number from {low} to {high}")
            return default
        return value

    rate = data.get("rate", {})
    if not isinstance(rate, dict) or set(rate) - {"max_actions", "per_minutes"}:
        problems.append("rate may hold only max_actions and per_minutes")
        rate = {}
    rate_max = whole(rate, "max_actions", 0, 0, 10_000)
    rate_minutes = whole(rate, "per_minutes", 60, 1, 24 * 60)

    approval = data.get("approval", {})
    known = {"ttl_minutes", "execute_within_minutes", "channels", "servicenow_group"}
    if not isinstance(approval, dict) or set(approval) - known:
        problems.append(f"approval may hold only {sorted(known)}")
        approval = {}
    ttl = whole(approval, "ttl_minutes", 60, 1, 7 * 24 * 60)
    within = whole(approval, "execute_within_minutes", 15, 1, 24 * 60)
    channels = approval.get("channels", [CHANNEL_UI])
    if (
        not isinstance(channels, list)
        or not channels
        or any(c not in CHANNELS for c in channels)
        or len(set(channels)) != len(channels)
    ):
        problems.append(f"approval.channels must be a list drawn from {list(CHANNELS)}")
        channels = [CHANNEL_UI]
    group = approval.get("servicenow_group", "")
    if not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9 ._\-]{0,100}", group):
        problems.append("approval.servicenow_group may hold letters, digits, spaces and . _ -")
        group = ""
    if CHANNEL_SERVICENOW in channels and not group:
        problems.append("approval.servicenow_group is required for the servicenow channel")

    if problems:
        raise PolicyError(problems)
    return Rules(
        sop_autonomy=sop_autonomy,
        raise_tier=raise_tier,
        automatic=automatic,
        freeze=tuple(freeze),
        hours=hours,
        rate_max=rate_max,
        rate_minutes=rate_minutes,
        approval_ttl_minutes=ttl,
        execute_within_minutes=within,
        channels=tuple(channels),
        servicenow_group=group,
    )


# ── The decision ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActionRequest:
    """What is about to be done. Everything here comes from the SOP version and the ticket."""

    sop_id: str
    sop_hash: str
    capability_id: str
    is_declared_write: bool
    base_tier: Optional[RiskTier]
    targets: Tuple[str, ...]  # the values of the capability's target parameters
    now: datetime


@dataclass(frozen=True)
class Situation:
    """What the decision depends on besides the request."""

    kill_switches: Tuple[str, ...]  # the names of the switches that are on
    tenant_autonomy: Autonomy
    rules: Rules
    max_tier: RiskTier  # the deployment's ceiling
    max_autonomy: Autonomy
    recent_actions: int  # executions started within the rate window
    refusals: Tuple[str, ...]  # why the connector would refuse the call; empty if it would not


@dataclass(frozen=True)
class Ruling:
    verdict: Verdict
    reason_code: str
    rule: str  # the rule that decided, in words, for the audit log and the approver
    tier: Optional[RiskTier]
    autonomy: Autonomy = Autonomy.SHADOW

    @property
    def is_kill_switch(self) -> bool:
        return self.reason_code == KILL_SWITCH


def effective_tier(request: ActionRequest, rules: Rules) -> Optional[RiskTier]:
    """The manifest's tier, raised to T2 when a tenant rule names any of the targets.

    A rule can only raise a tier. There is no way to express a lower one.
    """
    if request.base_tier is None:
        return None
    if any(r.touches(request.capability_id, request.targets) for r in rules.raise_tier):
        return RiskTier.T2
    return request.base_tier


def evaluate(request: ActionRequest, situation: Situation) -> Ruling:
    """Decide. The order is fixed (ADR 0008) and the first refusal wins."""
    rules = situation.rules
    tier = effective_tier(request, rules)

    def deny(code: str, rule: str) -> Ruling:
        return Ruling(Verdict.DENY, code, rule, tier)

    if situation.kill_switches:
        return deny(KILL_SWITCH, "kill switch is on: " + ", ".join(situation.kill_switches))
    if not request.is_declared_write or tier not in (RiskTier.T1, RiskTier.T2):
        # T3: anything that is not a declared write capability is never done.
        return deny(NOT_A_WRITE, f"'{request.capability_id}' is not a declared write capability")
    if tier is RiskTier.T2 and situation.max_tier is not RiskTier.T2:
        return deny(ABOVE_CEILING, f"tier {tier.value} is above this deployment's ceiling")
    if situation.max_tier is RiskTier.T0:
        return deny(ABOVE_CEILING, "this deployment makes no writes")
    for window in rules.freeze:
        if window.start <= request.now < window.end:
            return deny(CHANGE_FREEZE, f"change freeze until {window.end.isoformat()}")
    if rules.hours is not None and not rules.hours.permits(request.now):
        return deny(OUTSIDE_HOURS, "outside the hours in which writes are permitted")
    if situation.refusals:
        return deny(NOT_ON_ALLOW_LIST, "; ".join(situation.refusals)[:300])
    if rules.rate_max and situation.recent_actions >= rules.rate_max:
        return deny(
            RATE_LIMIT, f"{rules.rate_max} actions in {rules.rate_minutes} minutes already made"
        )

    level = min(
        situation.tenant_autonomy,
        rules.sop_level(request.sop_id, request.sop_hash),
        situation.max_autonomy,
        key=lambda a: a.rank,
    )
    if level is Autonomy.SHADOW:
        return Ruling(Verdict.SHADOW, AUTONOMY, "effective autonomy is shadow", tier, level)
    if level is Autonomy.RECOMMEND:
        return Ruling(Verdict.RECOMMEND, AUTONOMY, "effective autonomy is recommend", tier, level)
    if level is Autonomy.APPROVAL:
        return Ruling(
            Verdict.REQUIRE_APPROVAL, AUTONOMY, "effective autonomy is approval", tier, level
        )
    if tier is not RiskTier.T1:
        return Ruling(
            Verdict.REQUIRE_APPROVAL,
            TIER_NEEDS_APPROVAL,
            f"tier {tier.value} always needs an approval",
            tier,
            level,
        )
    if any(
        r.matches(request.capability_id, request.targets, request.sop_id) for r in rules.automatic
    ):
        return Ruling(
            Verdict.ALLOW, ON_AUTOMATIC_LIST, "on the tenant's automatic list", tier, level
        )
    return Ruling(
        Verdict.REQUIRE_APPROVAL,
        NOT_ON_AUTOMATIC_LIST,
        "not on the tenant's automatic list",
        tier,
        level,
    )
