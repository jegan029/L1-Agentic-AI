"""A tenant's model policy: what is accepted, and what it permits (ADR 0009)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.l1_agent.ai.policy import (
    MODEL_OFF,
    OFF,
    PROVIDER_NOT_ALLOWED,
    REGION_NOT_ALLOWED,
    RETENTION_NOT_ALLOWED,
    TASK_NOT_ENABLED,
    ModelPolicyError,
    parse_model_policy,
    permits,
)

RULES = {"providers": ["openai_compatible"], "tasks": ["rank", "interpret"]}


def provider(kind="openai_compatible", region="eu", retention=0):
    return SimpleNamespace(id=kind, region=region, retention_days=retention)


def problems_of(enabled, rules):
    with pytest.raises(ModelPolicyError) as err:
        parse_model_policy(enabled, rules)
    return " | ".join(err.value.problems)


class TestDefaults:
    def test_a_tenant_with_no_policy_uses_no_model(self):
        assert OFF.enabled is False
        assert permits(OFF, provider(), "rank") == MODEL_OFF

    def test_switching_on_needs_a_provider_and_a_task_named(self):
        text = problems_of(True, {})
        assert "providers" in text and "tasks" in text
        assert parse_model_policy(False, {}).enabled is False, "switched off needs neither"

    def test_budgets_have_defaults_and_no_cost_limit_unless_one_is_set(self):
        policy = parse_model_policy(True, RULES)
        assert (policy.incident_tokens, policy.daily_tokens) == (20_000, 1_000_000)
        assert policy.incident_cost_micros is None and policy.daily_cost_micros is None


class TestWhatIsAccepted:
    @pytest.mark.parametrize(
        "rules, expected",
        [
            ({**RULES, "providers": ["bedrock"]}, "'bedrock' is not one of"),
            ({**RULES, "tasks": ["decide"]}, "'decide' is not one of"),
            ({**RULES, "providers": "openai_compatible"}, "providers must be a list"),
            ({**RULES, "regions": [5]}, "regions must be a list"),
            ({**RULES, "max_retention_days": -1}, "max_retention_days"),
            ({**RULES, "max_retention_days": True}, "max_retention_days"),
            ({**RULES, "budgets": []}, "budgets must be an object"),
            ({**RULES, "budgets": {"incident_tokens": -5}}, "incident_tokens"),
            ({**RULES, "budgets": {"daily_tokens": 1.5}}, "daily_tokens"),
            ({**RULES, "budgets": {"daily_cost_usd": "ten"}}, "daily_cost_usd"),
            ({**RULES, "budgets": {"weekly_tokens": 5}}, "unknown budget 'weekly_tokens'"),
            ({**RULES, "autonomy": "automatic"}, "unknown rule 'autonomy'"),
        ],
    )
    def test_an_invalid_policy_is_refused_with_the_reason(self, rules, expected):
        assert expected in problems_of(True, rules)

    def test_every_problem_is_reported_at_once(self):
        text = problems_of(True, {"providers": ["x"], "tasks": ["y"], "budgets": {"z": 1}})
        assert text.count("|") >= 2

    def test_rules_must_be_an_object(self):
        assert "rules must be an object" in problems_of(True, ["rank"])

    def test_names_are_normalised_and_costs_are_kept_in_micro_dollars(self):
        policy = parse_model_policy(
            True,
            {
                "providers": ["Anthropic", "anthropic"],
                "tasks": ["RANK"],
                "regions": ["EU", "uk"],
                "max_retention_days": 30,
                "budgets": {"incident_cost_usd": 0.05, "daily_cost_usd": 20},
            },
        )
        assert policy.providers == ("anthropic",) and policy.tasks == ("rank",)
        assert policy.regions == ("eu", "uk") and policy.max_retention_days == 30
        assert (policy.incident_cost_micros, policy.daily_cost_micros) == (50_000, 20_000_000)


class TestWhatItPermits:
    def test_the_checks_in_order(self):
        policy = parse_model_policy(True, {**RULES, "regions": ["eu"], "max_retention_days": 30})
        assert permits(policy, provider(), "rank") is None
        assert permits(policy, provider(), "draft") == TASK_NOT_ENABLED
        assert permits(policy, provider(kind="anthropic"), "rank") == PROVIDER_NOT_ALLOWED
        assert permits(policy, provider(region="us"), "rank") == REGION_NOT_ALLOWED
        assert permits(policy, provider(retention=31), "rank") == RETENTION_NOT_ALLOWED
        assert permits(policy, provider(retention=30), "rank") is None

    def test_a_provider_that_states_no_region_or_retention_fails_a_policy_that_requires_one(self):
        regional = parse_model_policy(True, {**RULES, "regions": ["eu"]})
        assert permits(regional, provider(region=""), "rank") == REGION_NOT_ALLOWED
        retained = parse_model_policy(True, {**RULES, "max_retention_days": 0})
        assert permits(retained, provider(retention=None), "rank") == RETENTION_NOT_ALLOWED

    def test_no_region_or_retention_rule_means_no_restriction_on_them(self):
        policy = parse_model_policy(True, RULES)
        assert permits(policy, provider(region="", retention=None), "rank") is None

    def test_a_policy_that_is_switched_off_permits_nothing_whatever_its_rules(self):
        assert permits(parse_model_policy(False, RULES), provider(), "rank") == MODEL_OFF
