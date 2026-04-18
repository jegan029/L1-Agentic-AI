"""Unit tests for data models."""

from __future__ import annotations

from src.l1_agent.models.evidence import ExecutionOutcome, ExecutionSummary, StepResult, StepStatus
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP


class TestIncident:
    def test_from_servicenow(self):
        data = {
            "sys_id": "abc",
            "number": "INC001",
            "short_description": "Test",
            "description": "Details",
            "category": "Software",
            "priority": "2",
            "state": "1",
            "cmdb_ci": {"display_value": "MyApp", "value": "ref123"},
            "assignment_group": {"display_value": "L1-Team", "value": "ref456"},
        }
        inc = Incident.from_servicenow(data)
        assert inc.number == "INC001"
        assert inc.cmdb_ci == "MyApp"
        assert inc.assignment_group == "L1-Team"
        assert inc.correlation_id == "INC001"

    def test_to_dict(self):
        inc = Incident(sys_id="a", number="INC001", short_description="Test")
        d = inc.to_dict()
        assert d["number"] == "INC001"
        assert "sys_id" in d


class TestSOP:
    def test_from_dict(self):
        data = {
            "sop_id": "SOP-001",
            "title": "Test SOP",
            "keywords": ["test"],
            "steps": [
                {"step_id": "s1", "step_type": "NOTE", "parameters": {"text": "Hi"}},
            ],
            "escalation_criteria": {"max_retry_count": 3},
        }
        sop = SOP.from_dict(data)
        assert sop.sop_id == "SOP-001"
        assert len(sop.steps) == 1
        assert sop.escalation_criteria.max_retry_count == 3

    def test_to_dict_roundtrip(self):
        data = {
            "sop_id": "SOP-002",
            "title": "Roundtrip",
            "applicable_services": ["svc1"],
            "applicable_categories": ["cat1"],
            "applicable_assignment_groups": ["grp1"],
            "keywords": ["key1"],
            "steps": [
                {
                    "step_id": "s1",
                    "step_type": "MQ_CHECK",
                    "description": "Check queue",
                    "parameters": {"queue_manager": "QM1"},
                    "expected_output": "",
                    "on_success": "s2",
                    "on_failure": "",
                    "requires_approval": False,
                    "timeout_seconds": 60,
                }
            ],
            "pre_checks": ["check1"],
            "tools_required": ["IR360"],
            "escalation_criteria": {
                "max_retry_count": 2,
                "escalate_on_access_denied": True,
                "escalate_on_ambiguous_result": True,
                "escalate_on_write_action": True,
                "custom_rules": [],
            },
            "version": "1.0",
        }
        sop = SOP.from_dict(data)
        result = sop.to_dict()
        assert result["sop_id"] == "SOP-002"
        assert result["steps"][0]["on_success"] == "s2"


class TestStepResult:
    def test_to_audit_dict(self):
        result = StepResult(
            step_id="s1",
            step_type="SPLUNK_SEARCH",
            status=StepStatus.SUCCESS,
            evidence="Found 3 errors in the last hour",
            tool_called="Splunk",
            input_summary="query=...",
            output_summary="3 results",
        )
        audit = result.to_audit_dict()
        assert audit["step_id"] == "s1"
        assert audit["status"] == "success"
        assert "Found 3 errors" in audit["evidence_snippet"]


class TestExecutionSummary:
    def test_to_work_note(self):
        summary = ExecutionSummary(
            incident_number="INC001",
            sop_id="SOP-001",
            sop_title="Test SOP",
            outcome=ExecutionOutcome.RESOLVED,
            step_results=[
                StepResult(
                    step_id="s1",
                    step_type="NOTE",
                    status=StepStatus.SUCCESS,
                    output_summary="Done",
                ),
            ],
        )
        note = summary.to_work_note()
        assert "SOP Execution Summary" in note
        assert "RESOLVED" in note
        assert "[OK] s1" in note

    def test_to_escalation_packet(self):
        summary = ExecutionSummary(
            incident_number="INC001",
            sop_id="SOP-001",
            sop_title="Test SOP",
            outcome=ExecutionOutcome.ESCALATED,
            escalation_reason="Low confidence",
            step_results=[
                StepResult(
                    step_id="s1",
                    step_type="NOTE",
                    status=StepStatus.SUCCESS,
                    output_summary="Done",
                ),
            ],
        )
        packet = summary.to_escalation_packet()
        assert packet["incident_number"] == "INC001"
        assert packet["escalation_reason"] == "Low confidence"
        assert len(packet["checks_performed"]) == 1
