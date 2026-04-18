"""Unit tests for SOP parser."""

from __future__ import annotations

import json

from src.l1_agent.engine.sop_parser import SOPParser


class TestSOPParser:
    def setup_method(self):
        self.parser = SOPParser()

    def test_parse_json_sop(self):
        data = {
            "sop_id": "SOP-001",
            "title": "Test SOP",
            "keywords": ["test", "demo"],
            "steps": [
                {
                    "step_id": "s1",
                    "step_type": "NOTE",
                    "description": "Start",
                    "parameters": {"text": "Hello"},
                }
            ],
        }
        sop = self.parser.parse_from_json(data)
        assert sop.sop_id == "SOP-001"
        assert sop.title == "Test SOP"
        assert len(sop.steps) == 1
        assert sop.steps[0].step_type == "NOTE"

    def test_parse_json_string(self):
        data = {
            "sop_id": "SOP-002",
            "title": "JSON String SOP",
            "steps": [],
        }
        sop = self.parser.parse_from_json_string(json.dumps(data))
        assert sop is not None
        assert sop.sop_id == "SOP-002"

    def test_parse_invalid_json_string(self):
        result = self.parser.parse_from_json_string("not valid json")
        assert result is None

    def test_parse_from_servicenow_json_content(self):
        record = {
            "sys_id": "abc123",
            "sop_content": json.dumps({
                "sop_id": "SOP-003",
                "title": "From ServiceNow",
                "steps": [
                    {
                        "step_id": "s1",
                        "step_type": "SPLUNK_SEARCH",
                        "parameters": {"query": "index=main"},
                    }
                ],
            }),
        }
        sop = self.parser.parse_from_servicenow(record)
        assert sop is not None
        assert sop.sop_id == "SOP-003"
        assert len(sop.steps) == 1

    def test_parse_from_servicenow_field_mapped(self):
        record = {
            "sys_id": "def456",
            "short_description": "Handle MQ alerts",
            "category": "Middleware,MQ",
            "cmdb_ci": "PaymentService",
            "meta": "queue,mq,depth",
            "version": "2.0",
        }
        sop = self.parser.parse_from_servicenow(record)
        assert sop is not None
        assert sop.sop_id == "def456"
        assert sop.title == "Handle MQ alerts"
        assert "Middleware" in sop.applicable_categories
        assert "queue" in sop.keywords
        assert sop.version == "2.0"

    def test_parse_preserves_step_fields(self):
        data = {
            "sop_id": "SOP-004",
            "title": "Full Step Test",
            "steps": [
                {
                    "step_id": "s1",
                    "step_type": "MQ_CHECK",
                    "description": "Check depth",
                    "parameters": {"queue_manager": "QM1", "queue": "Q1", "action": "depth"},
                    "expected_output": "depth value",
                    "on_success": "s2",
                    "on_failure": "s-esc",
                    "requires_approval": False,
                    "timeout_seconds": 30,
                }
            ],
        }
        sop = self.parser.parse_from_json(data)
        step = sop.steps[0]
        assert step.step_id == "s1"
        assert step.parameters["queue_manager"] == "QM1"
        assert step.on_success == "s2"
        assert step.on_failure == "s-esc"
        assert step.timeout_seconds == 30

    def test_parse_escalation_criteria(self):
        data = {
            "sop_id": "SOP-005",
            "title": "With Escalation",
            "steps": [],
            "escalation_criteria": {
                "max_retry_count": 3,
                "escalate_on_access_denied": True,
                "escalate_on_write_action": True,
            },
        }
        sop = self.parser.parse_from_json(data)
        assert sop.escalation_criteria.max_retry_count == 3
        assert sop.escalation_criteria.escalate_on_access_denied is True
