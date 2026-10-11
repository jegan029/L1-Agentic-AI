"""Unit tests for SOP matching engine."""

from __future__ import annotations

from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP


def _make_incident(**overrides) -> Incident:
    defaults = {
        "sys_id": "inc-001",
        "number": "INC001",
        "short_description": "MQ queue depth high on PAYMENT.REQUEST",
        "description": "Messages not being consumed on queue manager QMPROD01",
        "category": "Middleware",
        "subcategory": "MQ",
        "cmdb_ci": "PaymentService",
        "assignment_group": "L1-Middleware-Support",
        "priority": 2,
        "state": 1,
    }
    defaults.update(overrides)
    return Incident(**defaults)


def _make_sop(**overrides) -> SOP:
    defaults = {
        "sop_id": "SOP-MQ-001",
        "title": "MQ Queue Depth High Investigation",
        "applicable_services": ["PaymentService"],
        "applicable_categories": ["Middleware"],
        "applicable_assignment_groups": ["L1-Middleware-Support"],
        "keywords": ["queue depth", "mq", "messages not consumed"],
    }
    defaults.update(overrides)
    return SOP(**defaults)


class TestSOPMatcher:
    def test_exact_match_high_confidence(self):
        matcher = SOPMatcher(confidence_threshold=0.6)
        incident = _make_incident()
        sop = _make_sop()
        result = matcher.match(incident, [sop])

        assert result.sop is not None
        assert result.sop.sop_id == "SOP-MQ-001"
        assert result.confidence >= 0.6

    def test_no_sops_returns_none(self):
        matcher = SOPMatcher(confidence_threshold=0.6)
        incident = _make_incident()
        result = matcher.match(incident, [])

        assert result.sop is None
        assert result.confidence == 0.0
        assert "No SOPs available" in result.rationale

    def test_low_confidence_below_threshold(self):
        matcher = SOPMatcher(confidence_threshold=0.9)
        incident = _make_incident(
            short_description="Unrelated issue with disk space",
            category="Storage",
            cmdb_ci="FileServer",
            assignment_group="L1-Storage",
        )
        sop = _make_sop()
        result = matcher.match(incident, [sop])

        assert result.confidence < 0.9
        assert "Low confidence" in result.rationale

    def test_keyword_match_in_description(self):
        matcher = SOPMatcher(confidence_threshold=0.3)
        incident = _make_incident(
            short_description="Issue with payment processing",
            description="MQ queue depth seems high",
        )
        sop = _make_sop(keywords=["queue depth", "payment"])
        result = matcher.match(incident, [sop])

        assert result.sop is not None
        assert result.confidence > 0.0

    def test_category_match_boosts_score(self):
        matcher = SOPMatcher(confidence_threshold=0.1)
        incident = _make_incident()
        sop_match = _make_sop(applicable_categories=["Middleware"])
        sop_nomatch = _make_sop(
            sop_id="SOP-OTHER",
            applicable_categories=["Network"],
            keywords=["queue depth"],
        )
        result = matcher.match(incident, [sop_match, sop_nomatch])

        assert result.sop is not None
        assert result.sop.sop_id == "SOP-MQ-001"

    def test_ci_match_boosts_score(self):
        matcher = SOPMatcher(confidence_threshold=0.1)
        incident = _make_incident(cmdb_ci="PaymentService")
        sop_match = _make_sop(applicable_services=["PaymentService"])
        sop_nomatch = _make_sop(
            sop_id="SOP-OTHER",
            applicable_services=["OrderService"],
            keywords=["queue depth"],
        )
        result = matcher.match(incident, [sop_match, sop_nomatch])

        assert result.sop is not None
        assert result.sop.sop_id == "SOP-MQ-001"

    def test_assignment_group_match(self):
        matcher = SOPMatcher(confidence_threshold=0.1)
        incident = _make_incident(assignment_group="L1-Middleware-Support")
        sop = _make_sop(applicable_assignment_groups=["L1-Middleware-Support"])
        result = matcher.match(incident, [sop])

        assert result.confidence > 0.0

    def test_multiple_sops_best_selected(self):
        matcher = SOPMatcher(confidence_threshold=0.1)
        incident = _make_incident()
        sop1 = _make_sop(sop_id="SOP-1", keywords=["queue depth", "mq", "payment"])
        sop2 = _make_sop(
            sop_id="SOP-2",
            keywords=["disk space"],
            applicable_categories=["Storage"],
            applicable_services=["FileServer"],
        )
        result = matcher.match(incident, [sop1, sop2])

        assert result.sop is not None
        assert result.sop.sop_id == "SOP-1"

    def test_alternatives_populated(self):
        matcher = SOPMatcher(confidence_threshold=0.1)
        incident = _make_incident()
        sop1 = _make_sop(sop_id="SOP-1", keywords=["queue depth", "mq"])
        sop2 = _make_sop(sop_id="SOP-2", keywords=["queue", "payment"])
        result = matcher.match(incident, [sop1, sop2])

        assert result.sop is not None
        # alternatives should include the other SOP if it scored > 0
        assert isinstance(result.alternatives, list)

    def test_regex_keyword_support(self):
        matcher = SOPMatcher(confidence_threshold=0.1)
        incident = _make_incident(short_description="MQ queue depth 5000 exceeded")
        sop = _make_sop(keywords=[r"queue depth \d+"])
        result = matcher.match(incident, [sop])

        assert result.confidence > 0.0


SAMPLE_MQ = {
    "title": "MQ Queue Depth High - Investigation and Remediation",
    "keywords": ["queue depth", "mq", "messages not consumed", "queue high", "payment"],
    "applicable_categories": ["Middleware", "MQ"],
    "applicable_services": ["PaymentService", "OrderService"],
}


class TestSignalsThatCannotBeCompared:
    """A ticket that lacks a field is not scored as if the field were wrong."""

    def test_ticket_from_a_stock_instance_clears_the_default_threshold(self):
        # What a ServiceNow developer instance returned: no CI, and the default
        # category in place of the one the ticket was created with.
        incident = _make_incident(
            short_description="MQ queue depth high on ORDER.REQUEST",
            description="Queue depth alert for ORDER.REQUEST on queue manager QMPROD01.",
            category="Inquiry / Help",
            subcategory="",
            cmdb_ci="",
        )
        result = SOPMatcher(confidence_threshold=0.6).match(incident, [_make_sop(**SAMPLE_MQ)])

        assert result.confidence >= 0.6
        assert "not compared: ci" in result.rationale

    def test_missing_ci_scores_higher_than_wrong_ci(self):
        matcher = SOPMatcher()
        sop = _make_sop()
        missing = matcher.match(_make_incident(cmdb_ci=""), [sop]).confidence
        wrong = matcher.match(_make_incident(cmdb_ci="FileServer"), [sop]).confidence
        right = matcher.match(_make_incident(cmdb_ci="PaymentService"), [sop]).confidence

        assert wrong < missing <= right

    def test_wrong_category_still_counts_against_the_match(self):
        matcher = SOPMatcher()
        sop = _make_sop()
        wrong = matcher.match(_make_incident(category="Storage", subcategory=""), [sop])
        right = matcher.match(_make_incident(), [sop])

        assert wrong.confidence < right.confidence

    def test_sop_that_declares_no_services_is_not_penalised_for_it(self):
        incident = _make_incident()
        with_services = _make_sop(**SAMPLE_MQ)
        without = _make_sop(**{**SAMPLE_MQ, "applicable_services": []})
        declared = SOPMatcher().match(incident, [with_services]).confidence
        undeclared = SOPMatcher().match(incident, [without]).confidence

        assert undeclared == declared == 1.0

    def test_matching_fields_alone_do_not_clear_the_threshold(self):
        incident = _make_incident(short_description="Something is wrong", description="")
        result = SOPMatcher(confidence_threshold=0.6).match(incident, [_make_sop(**SAMPLE_MQ)])

        assert result.confidence < 0.6

    def test_score_stays_within_bounds(self):
        for incident in (
            _make_incident(),
            _make_incident(category="", subcategory="", cmdb_ci="", assignment_group=""),
            _make_incident(short_description="", description=""),
        ):
            for sop in (_make_sop(), _make_sop(keywords=[], title=""), _make_sop(**SAMPLE_MQ)):
                assert 0.0 <= SOPMatcher().match(incident, [sop]).confidence <= 1.0


class TestKeywordEvidence:
    def test_long_keyword_list_does_not_dilute_a_clear_match(self):
        incident = _make_incident(category="", subcategory="", cmdb_ci="", assignment_group="")
        short = _make_sop(keywords=["queue depth", "mq", "payment"])
        long = _make_sop(
            keywords=["queue depth", "mq", "payment", "backlog", "consumer lag", "dlq", "poison"]
        )

        assert SOPMatcher().match(incident, [long]).confidence == 1.0
        assert SOPMatcher().match(incident, [short]).confidence == 1.0

    def test_one_generic_keyword_is_not_enough(self):
        incident = _make_incident(
            short_description="Payment page is slow for users",
            description="",
            category="",
            subcategory="",
            cmdb_ci="",
        )
        result = SOPMatcher(confidence_threshold=0.6).match(incident, [_make_sop(**SAMPLE_MQ)])

        assert 0.0 < result.confidence < 0.6

    def test_keyword_matches_whole_words_only(self):
        sop = _make_sop(title="", keywords=["ram"], applicable_assignment_groups=[])
        inside = _make_incident(short_description="Program parameter error", description="")
        whole = _make_incident(short_description="RAM exhausted on host", description="")
        blank = dict(category="", subcategory="", cmdb_ci="")

        assert (
            SOPMatcher().match(_make_incident(**{**inside.__dict__, **blank}), [sop]).confidence
            == 0.0
        )
        assert (
            SOPMatcher().match(_make_incident(**{**whole.__dict__, **blank}), [sop]).confidence
            == 1.0
        )

    def test_title_stopwords_and_punctuation_are_not_evidence(self):
        sop = _make_sop(title="Reset - and Restart", keywords=[], applicable_assignment_groups=[])
        incident = _make_incident(
            short_description="Standard request - handled by operations",
            description="",
            category="",
            subcategory="",
            cmdb_ci="",
        )

        assert SOPMatcher().match(incident, [sop]).confidence == 0.0

    def test_invalid_regex_keyword_falls_back_to_literal_text(self):
        sop = _make_sop(title="", keywords=["depth (high"])
        incident = _make_incident(short_description="queue depth (high on QM1")

        assert SOPMatcher().match(incident, [sop]).confidence > 0.5
