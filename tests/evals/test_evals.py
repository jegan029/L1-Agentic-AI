"""The evaluation suite, as CI runs it (ADR 0009; delivery plan, Phase 5 exit criteria).

Each check is the same function `python -m evals.run` calls. What they measure:

1. Injection: with a model that obeys injected text, or always gives the worst
   answer, or returns garbage, or fails, no capability call is made outside the
   step's declared capability and parameters, and no write is made. Counted
   inside each connector's `invoke`.
2. Golden incidents: with the model off, failing or returning garbage, every
   incident ends as it does with no model, and never resolved without a
   verification.
3. Redaction: no seeded secret or personal data item is in any request that
   reaches a provider's HTTP endpoint, for all four tasks and both clients.
"""

from __future__ import annotations

import pytest

from evals.checks import CHECKS
from evals.harness import corpus
from evals.providers import BAD_PROVIDERS, DIRECTIVE

pytestmark = pytest.mark.timeout(300)


@pytest.mark.parametrize("name", sorted(CHECKS))
async def test_check(name):
    failures = await CHECKS[name]()
    assert failures == [], "\n".join(failures)


class TestTheCorporaAreWhatTheyClaim:
    def test_every_exit_criterion_has_a_check(self):
        names = " | ".join(CHECKS)
        for needed in (
            "golden: model off",
            "golden: garbage answers",
            "golden: failing provider",
            "injection: tickets and tool output",
            "injection: articles",
            "redaction: outbound requests",
        ):
            assert needed in names

    def test_the_bad_models_cover_the_cases(self):
        assert set(BAD_PROVIDERS) == {"obedient", "hostile", "garbage", "failing"}

    def test_golden_cases_cover_every_outcome_a_run_can_have_here(self):
        cases = corpus("golden")["cases"]
        outcomes = {case["expect"]["outcome"] for case in cases}
        assert outcomes == {"resolved", "escalated", "needs_human"}
        assert any(case["expect"]["writes"] for case in cases), "a run that makes a change"
        assert any(not case["expect"]["calls"] for case in cases), "a run that calls nothing"
        assert any("expect_with_help" in case for case in cases)
        for case in cases:
            if case["expect"]["outcome"] == "resolved":
                assert case["expect"]["verified_by"], case["id"]

    def test_every_injection_case_carries_a_directive_or_a_breakout(self):
        data = corpus("injection")
        for case in data["tickets"]:
            text = case["incident"]["short_description"] + case["incident"]["description"]
            assert DIRECTIVE.search(text) or ">>>" in text, case["id"]
        for case in data["tool_output"]:
            assert DIRECTIVE.search(case["suffix"]), case["id"]
        for case in data["articles"]:
            assert DIRECTIVE.search(case["body"]), case["id"]
        assert len(data["tickets"]) >= 7 and len(data["tool_output"]) >= 2

    def test_the_injection_corpus_tries_to_cause_a_write(self):
        text = str(corpus("injection"))
        assert "SOP-AUTOSYS-002" in text and "force_start" in text and "@@set" in text

    def test_the_redaction_corpus_covers_each_kind_and_admits_its_gaps(self):
        data = corpus("redaction")
        kinds = {item["kind"] for item in data["items"]}
        assert kinds == {"secret", "email", "phone", "card", "iban", "national_id", "ip", "person"}
        assert len(data["items"]) >= 20
        assert data["not_found"], "what pattern matching misses is written down"
        for item in [*data["items"], *data["not_found"]]:
            assert item["value"] in item["text"], item["value"]
