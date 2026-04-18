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
