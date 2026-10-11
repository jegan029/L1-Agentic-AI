"""The policy decision (ADR 0008): a pure function, checked in a fixed order.

Decision D5 is pinned here: nothing above T1 is ever allowed without an approval.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.l1_agent.actions import policy as p
from src.l1_agent.actions.policy import (
    ActionRequest,
    Autonomy,
    PolicyError,
    Rules,
    Situation,
    Verdict,
    evaluate,
    parse_rules,
)
from src.l1_agent.connectors.sdk.manifest import RiskTier

CAP = "autosys.job.force_start"
SOP = "SOP-RESTART"
HASH = "a" * 64
NOON_TUESDAY = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
AUTOMATIC_RULES = {
    "sop_autonomy": {SOP: {"level": "automatic", "content_hash": HASH}},
    "automatic": [{"sop_id": SOP, "capability": CAP, "targets": ["NIGHTLY_*"]}],
}


def request(**changes) -> ActionRequest:
    base = ActionRequest(
        sop_id=SOP,
        sop_hash=HASH,
        capability_id=CAP,
        is_declared_write=True,
        base_tier=RiskTier.T1,
        targets=("NIGHTLY_LOAD",),
        now=NOON_TUESDAY,
    )
    return replace(base, **changes)


def situation(rules=None, **changes) -> Situation:
    base = Situation(
        kill_switches=(),
        tenant_autonomy=Autonomy.AUTOMATIC,
        rules=parse_rules(AUTOMATIC_RULES if rules is None else rules),
        max_tier=RiskTier.T2,
        max_autonomy=Autonomy.AUTOMATIC,
        recent_actions=0,
        refusals=(),
    )
    return replace(base, **changes)


class TestDefaults:
    def test_a_tenant_with_no_policy_is_in_shadow(self):
        ruling = evaluate(request(), situation({}, tenant_autonomy=Autonomy.SHADOW))
        assert ruling.verdict is Verdict.SHADOW

    def test_a_tenant_level_alone_is_not_enough_the_sop_version_is_in_shadow_too(self):
        ruling = evaluate(request(), situation({}))
        assert ruling.verdict is Verdict.SHADOW, "a new SOP version starts in shadow"

    def test_everything_lined_up_allows_a_t1_write(self):
        ruling = evaluate(request(), situation())
        assert (ruling.verdict, ruling.reason_code) == (Verdict.ALLOW, p.ON_AUTOMATIC_LIST)
        assert ruling.tier is RiskTier.T1


class TestAutonomy:
    @pytest.mark.parametrize(
        "tenant, sop, ceiling, expected",
        [
            ("shadow", "automatic", "automatic", Verdict.SHADOW),
            ("automatic", "shadow", "automatic", Verdict.SHADOW),
            ("automatic", "automatic", "shadow", Verdict.SHADOW),
            ("recommend", "automatic", "automatic", Verdict.RECOMMEND),
            ("automatic", "recommend", "approval", Verdict.RECOMMEND),
            ("approval", "automatic", "automatic", Verdict.REQUIRE_APPROVAL),
            ("automatic", "automatic", "approval", Verdict.REQUIRE_APPROVAL),
            ("automatic", "automatic", "automatic", Verdict.ALLOW),
        ],
    )
    def test_the_effective_level_is_the_lowest_of_the_three(self, tenant, sop, ceiling, expected):
        rules = {
            **AUTOMATIC_RULES,
            "sop_autonomy": {SOP: {"level": sop, "content_hash": HASH}},
        }
        ruling = evaluate(
            request(),
            situation(rules, tenant_autonomy=Autonomy(tenant), max_autonomy=Autonomy(ceiling)),
        )
        assert ruling.verdict is expected

    def test_the_sop_level_is_for_one_version_only(self):
        ruling = evaluate(request(sop_hash="b" * 64), situation())
        assert ruling.verdict is Verdict.SHADOW, "a newly published version starts in shadow"

    def test_automatic_needs_the_target_on_the_automatic_list(self):
        ruling = evaluate(request(targets=("WEEKLY_LOAD",)), situation())
        assert (ruling.verdict, ruling.reason_code) == (
            Verdict.REQUIRE_APPROVAL,
            p.NOT_ON_AUTOMATIC_LIST,
        )

    def test_an_automatic_rule_is_for_one_sop(self):
        rules = {**AUTOMATIC_RULES, "sop_autonomy": {"OTHER": AUTOMATIC_RULES["sop_autonomy"][SOP]}}
        ruling = evaluate(request(sop_id="OTHER"), situation(rules))
        assert ruling.verdict is Verdict.REQUIRE_APPROVAL

    def test_every_target_must_be_covered_for_automatic(self):
        ruling = evaluate(request(targets=("NIGHTLY_LOAD", "WEEKLY_LOAD")), situation())
        assert ruling.verdict is Verdict.REQUIRE_APPROVAL


class TestTiers:
    """Decision D5."""

    def test_t2_always_needs_an_approval_even_on_the_automatic_list(self):
        ruling = evaluate(request(base_tier=RiskTier.T2), situation())
        assert (ruling.verdict, ruling.reason_code) == (
            Verdict.REQUIRE_APPROVAL,
            p.TIER_NEEDS_APPROVAL,
        )

    def test_a_tenant_rule_raises_the_tier_for_named_targets(self):
        rules = {**AUTOMATIC_RULES, "raise_tier": [{"capability": CAP, "targets": ["NIGHTLY_L*"]}]}
        ruling = evaluate(request(), situation(rules))
        assert ruling.tier is RiskTier.T2 and ruling.verdict is Verdict.REQUIRE_APPROVAL
        untouched = evaluate(request(targets=("NIGHTLY_EXTRACT",)), situation(rules))
        assert untouched.tier is RiskTier.T1 and untouched.verdict is Verdict.ALLOW

    def test_one_raised_target_among_several_raises_the_action(self):
        rules = {
            "sop_autonomy": AUTOMATIC_RULES["sop_autonomy"],
            "automatic": [{"sop_id": SOP, "capability": CAP, "targets": ["*"]}],
            "raise_tier": [{"capability": CAP, "targets": ["PAYROLL_*"]}],
        }
        ruling = evaluate(request(targets=("NIGHTLY_LOAD", "PAYROLL_POST")), situation(rules))
        assert ruling.tier is RiskTier.T2

    def test_there_is_no_way_to_write_a_rule_that_lowers_a_tier(self):
        for bad in (
            {"lower_tier": [{"capability": CAP, "targets": ["*"]}]},
            {"raise_tier": [{"capability": CAP, "targets": ["*"], "tier": "T1"}]},
        ):
            with pytest.raises(PolicyError):
                parse_rules(bad)
        rules = {**AUTOMATIC_RULES, "raise_tier": [{"capability": CAP, "targets": ["*"]}]}
        assert evaluate(request(base_tier=RiskTier.T2), situation(rules)).tier is RiskTier.T2

    def test_the_deployment_ceiling_refuses_what_is_above_it(self):
        t2 = evaluate(request(base_tier=RiskTier.T2), situation(max_tier=RiskTier.T1))
        none = evaluate(request(), situation(max_tier=RiskTier.T0))
        assert (t2.verdict, t2.reason_code) == (Verdict.DENY, p.ABOVE_CEILING)
        assert (none.verdict, none.reason_code) == (Verdict.DENY, p.ABOVE_CEILING)

    def test_something_that_is_not_a_declared_write_is_never_done(self):
        for req in (
            request(is_declared_write=False),
            request(base_tier=None),
            request(base_tier=RiskTier.T0),
        ):
            ruling = evaluate(req, situation())
            assert (ruling.verdict, ruling.reason_code) == (Verdict.DENY, p.NOT_A_WRITE)


class TestRefusals:
    def test_the_kill_switch_is_checked_before_anything_else(self):
        ruling = evaluate(
            request(is_declared_write=False),
            situation(kill_switches=("tenant",), refusals=("not allowed",), recent_actions=99),
        )
        assert ruling.reason_code == p.KILL_SWITCH and ruling.is_kill_switch

    def test_the_order_of_the_checks_is_fixed(self):
        """Each case switches on one more refusal; the earlier one in the order still decides."""
        rules = {
            **AUTOMATIC_RULES,
            "freeze": [{"start": "2026-03-01T00:00:00Z", "end": "2026-03-10T00:00:00Z"}],
            "hours": {"days": [5, 6], "start": "00:00", "end": "23:59"},
            "rate": {"max_actions": 1, "per_minutes": 60},
        }
        everything = situation(rules, recent_actions=5, refusals=("not on allow list",))
        steps = [
            (dict(), p.CHANGE_FREEZE),
            (dict(freeze=()), p.OUTSIDE_HOURS),
            (dict(freeze=(), hours=None), p.NOT_ON_ALLOW_LIST),
        ]
        for changed, expected in steps:
            ruling = evaluate(
                request(), replace(everything, rules=replace(everything.rules, **changed))
            )
            assert (ruling.verdict, ruling.reason_code) == (Verdict.DENY, expected)
        cleared = replace(everything.rules, freeze=(), hours=None)
        ruling = evaluate(request(), replace(everything, rules=cleared, refusals=()))
        assert ruling.reason_code == p.RATE_LIMIT
        ruling = evaluate(
            request(), replace(everything, rules=cleared, refusals=(), recent_actions=0)
        )
        assert ruling.verdict is Verdict.ALLOW

    def test_a_freeze_window_denies_only_inside_it(self):
        rules = {
            **AUTOMATIC_RULES,
            "freeze": [{"start": "2026-03-03T11:00:00+00:00", "end": "2026-03-03T13:00:00+00:00"}],
        }
        inside = evaluate(request(), situation(rules))
        after = evaluate(
            request(now=datetime(2026, 3, 3, 13, 0, tzinfo=timezone.utc)), situation(rules)
        )
        assert inside.reason_code == p.CHANGE_FREEZE and after.verdict is Verdict.ALLOW

    @pytest.mark.parametrize(
        "hours, allowed",
        [
            ({"days": [1], "start": "06:00", "end": "22:00"}, True),
            ({"days": [0, 2, 3, 4], "start": "06:00", "end": "22:00"}, False),
            ({"days": [1], "start": "13:00", "end": "22:00"}, False),
            ({"days": [1], "start": "22:00", "end": "13:00"}, True),  # crosses midnight
            ({"days": [1], "start": "06:00", "end": "12:00"}, False),  # the end is exclusive
        ],
    )
    def test_permitted_hours(self, hours, allowed):
        ruling = evaluate(request(), situation({**AUTOMATIC_RULES, "hours": hours}))
        assert (ruling.verdict is Verdict.ALLOW) is allowed
        if not allowed:
            assert ruling.reason_code == p.OUTSIDE_HOURS

    def test_a_denial_in_shadow_mode_is_still_a_denial(self):
        """Policy refuses before autonomy is looked at, so shadow does not hide a refusal."""
        ruling = evaluate(
            request(), situation(tenant_autonomy=Autonomy.SHADOW, refusals=("empty allow list",))
        )
        assert ruling.verdict is Verdict.DENY


class TestRulesAreValidated:
    def test_empty_rules_are_the_defaults(self):
        assert parse_rules(None) == parse_rules({}) == Rules()
        assert Rules().channels == ("ui",) and Rules().approval_ttl_minutes == 60

    @pytest.mark.parametrize(
        "rules, expected",
        [
            ([], "rules must be an object"),
            ({"surprise": 1}, "unknown rule 'surprise'"),
            ({"sop_autonomy": {SOP: {"level": "automatic"}}}, "content_hash"),
            ({"sop_autonomy": {SOP: {"level": "yolo", "content_hash": HASH}}}, "needs a level"),
            ({"automatic": [{"capability": CAP, "targets": ["*"]}]}, "sop_id is required"),
            ({"automatic": [{"sop_id": SOP, "capability": CAP, "targets": []}]}, "non-empty"),
            ({"automatic": [{"sop_id": SOP, "capability": CAP}]}, "non-empty"),
            ({"raise_tier": [{"targets": ["*"]}]}, "capability is required"),
            (
                {"freeze": [{"start": "2026-01-02T00:00:00Z", "end": "2026-01-01T00:00:00Z"}]},
                "freeze[0]",
            ),
            (
                {"freeze": [{"start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00"}]},
                "offset",
            ),
            ({"hours": {"days": [7], "start": "06:00", "end": "22:00"}}, "hours needs"),
            ({"hours": {"start": "6am", "end": "22:00"}}, "hours needs"),
            ({"rate": {"max_actions": -1}}, "max_actions"),
            ({"rate": {"max_actions": True}}, "max_actions"),
            ({"approval": {"ttl_minutes": 0}}, "ttl_minutes"),
            ({"approval": {"channels": ["email"]}}, "channels"),
            ({"approval": {"channels": []}}, "channels"),
            ({"approval": {"channels": ["servicenow"]}}, "servicenow_group is required"),
            ({"approval": {"channels": ["ui"], "servicenow_group": "x^y"}}, "servicenow_group"),
        ],
    )
    def test_a_bad_rule_is_named(self, rules, expected):
        with pytest.raises(PolicyError) as err:
            parse_rules(rules)
        assert any(expected in problem for problem in err.value.problems), err.value.problems

    def test_every_problem_is_reported_at_once(self):
        with pytest.raises(PolicyError) as err:
            parse_rules({"surprise": 1, "rate": {"max_actions": -1}, "hours": {"days": []}})
        assert len(err.value.problems) == 3


# ── Properties over every combination ─────────────────────────────────

_levels = st.sampled_from(list(Autonomy))
_tiers = st.sampled_from([None, RiskTier.T0, RiskTier.T1, RiskTier.T2])


@st.composite
def cases(draw):
    target = draw(st.sampled_from(["NIGHTLY_LOAD", "PAYROLL_POST", "WEEKLY_LOAD"]))
    rules = {
        "sop_autonomy": {
            SOP: {
                "level": draw(_levels).value,
                "content_hash": draw(st.sampled_from([HASH, "b" * 64])),
            }
        },
        "automatic": draw(
            st.sampled_from(
                [
                    [],
                    [{"sop_id": SOP, "capability": CAP, "targets": ["NIGHTLY_*"]}],
                    [{"sop_id": SOP, "capability": CAP, "targets": ["*"]}],
                    [{"sop_id": "OTHER", "capability": CAP, "targets": ["*"]}],
                ]
            )
        ),
        "raise_tier": draw(st.sampled_from([[], [{"capability": CAP, "targets": ["PAYROLL_*"]}]])),
        "rate": {"max_actions": draw(st.integers(0, 3)), "per_minutes": 60},
    }
    req = request(
        base_tier=draw(_tiers),
        is_declared_write=draw(st.booleans()),
        targets=(target,),
    )
    sit = situation(
        rules,
        kill_switches=draw(st.sampled_from([(), ("tenant",), ("global", "local")])),
        tenant_autonomy=draw(_levels),
        max_autonomy=draw(_levels),
        max_tier=draw(st.sampled_from([RiskTier.T0, RiskTier.T1, RiskTier.T2])),
        recent_actions=draw(st.integers(0, 4)),
        refusals=draw(st.sampled_from([(), ("refused",)])),
    )
    return req, sit, rules


class TestProperties:
    @given(cases())
    def test_what_must_hold_for_any_request(self, case):
        req, sit, rules = case
        ruling = evaluate(req, sit)
        assert evaluate(req, sit) == ruling, "the same inputs give the same ruling"
        if sit.kill_switches:
            assert ruling.verdict is Verdict.DENY and ruling.is_kill_switch
        if sit.refusals or not req.is_declared_write:
            assert ruling.verdict is Verdict.DENY
        if ruling.verdict is Verdict.ALLOW:
            # D5 and everything else an unattended write depends on, all at once.
            assert ruling.tier is RiskTier.T1 and req.base_tier is RiskTier.T1
            assert not sit.kill_switches and not sit.refusals and req.is_declared_write
            assert sit.tenant_autonomy is Autonomy.AUTOMATIC
            assert sit.max_autonomy is Autonomy.AUTOMATIC
            assert sit.max_tier in (RiskTier.T1, RiskTier.T2)
            assert rules["sop_autonomy"][SOP] == {"level": "automatic", "content_hash": HASH}
            assert any(r["sop_id"] == SOP for r in rules["automatic"])
            assert not req.targets[0].startswith("PAYROLL_"), "a raised target is T2"
            assert not rules["rate"]["max_actions"] or (
                sit.recent_actions < rules["rate"]["max_actions"]
            )
        if ruling.tier is RiskTier.T2:
            assert ruling.verdict is not Verdict.ALLOW
