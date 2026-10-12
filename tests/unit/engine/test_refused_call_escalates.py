"""A call a connector refuses ends the run as ESCALATED, and is never tried again.

A refused call (`AdapterResult.escalate`) is not a failed check. It does not
follow the step's `on_failure` edge, because that edge is for "the check ran
and found a problem" and may lead somewhere a refusal must not.
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.adapters.mock_adapters import MockAutosysAdapter
from src.l1_agent.adapters.splunk.adapter import SplunkSearchAdapter
from src.l1_agent.adapters.splunk.backend import AccessDenied, Unavailable
from src.l1_agent.adapters.splunk.guard import GuardConfig
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.models.evidence import ExecutionOutcome, StepResult, StepStatus
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, SOPStep
from src.l1_agent.utils.retry import CircuitBreaker, CircuitState

INCIDENT = Incident(sys_id="inc-1", number="INC1", short_description="Job BATCH_PAYMENT failed")


class Backend:
    name = "mcp"

    def __init__(self, error: Exception = None) -> None:
        self.error = error
        self.searches = 0

    async def search(self, search: Any) -> List[Dict[str, Any]]:
        self.searches += 1
        if self.error:
            raise self.error
        return [{"_raw": "ERROR BATCH_PAYMENT exit code 1", "host": "batch-01"}]

    async def health(self):
        return True, ""

    async def close(self) -> None:
        return None


def splunk(backend: Backend) -> SplunkSearchAdapter:
    return SplunkSearchAdapter(backend, GuardConfig(allowed_indexes=("batch_logs",)))


def sop(query: str, *, verify: bool = False) -> SOP:
    """A search whose failure edge leads to a verified RESOLVE: the edge a refusal must not take."""
    steps = [
        {
            "step_id": "search",
            "step_type": "SPLUNK_SEARCH",
            "parameters": {"query": query, "time_range": {"earliest": "-4h", "latest": "now"}},
            "on_success": "handover",
            "on_failure": "verify",
        },
        {
            "step_id": "verify",
            "step_type": "VERIFY",
            "parameters": {
                "check": "SPLUNK_SEARCH" if verify else "AUTOSYS_STATUS",
                "parameters": {"query": query}
                if verify
                else {"job_name": "BATCH_PAYMENT", "query_type": "status"},
                "assert": "result.result_count == 0" if verify else "result.jobs[0].status == 'FA'",
                "retries": 3,
            },
            "on_success": "resolve",
            "on_failure": "handover",
        },
        {"step_id": "resolve", "step_type": "RESOLVE", "parameters": {"text": "No errors left"}},
        {
            "step_id": "handover",
            "step_type": "ESCALATE",
            "parameters": {"reason": "Evidence collected"},
        },
    ]
    return SOP(sop_id="SOP-T", title="t", steps=[SOPStep.from_dict(s) for s in steps])


def executor(backend: Backend, **kwargs) -> SOPExecutor:
    adapters = {"splunk": splunk(backend), "autosys": MockAutosysAdapter()}
    return SOPExecutor(adapters=adapters, retry_base_delay=0, **kwargs)


class TestARefusedSearch:
    async def test_a_search_the_guard_refuses_escalates_and_takes_no_edge(self):
        backend = Backend()
        summary = await executor(backend).execute_sop(INCIDENT, sop("index=_internal ERROR"))
        assert summary.outcome == ExecutionOutcome.ESCALATED
        assert backend.searches == 0
        # The failure edge leads to a RESOLVE. It was not followed: the run stopped at the search.
        assert [r.step_id for r in summary.step_results] == ["search"]
        step = summary.step_results[0]
        assert step.status == StepStatus.ESCALATED
        assert "Search refused by the guard" in step.error_message
        assert "'_internal' is not on the allow list" in summary.escalation_reason
        assert step.audit["outcome"] == "rejected" and step.audit["backend"] == "mcp"

    async def test_a_refused_command_escalates_too(self):
        summary = await executor(Backend()).execute_sop(INCIDENT, sop("index=batch_logs | delete"))
        assert summary.outcome == ExecutionOutcome.ESCALATED
        assert "'delete' is not allowed" in summary.escalation_reason

    async def test_access_denied_is_asked_once_and_escalates(self):
        backend = Backend(AccessDenied("Splunk refused the credential (HTTP 403)"))
        breaker = CircuitBreaker(failure_threshold=1)
        run = executor(backend, retry_max_attempts=3, circuit_breakers={"splunk": breaker})
        summary = await run.execute_sop(INCIDENT, sop("index=batch_logs ERROR"))
        assert summary.outcome == ExecutionOutcome.ESCALATED
        assert backend.searches == 1
        assert "HTTP 403" in summary.escalation_reason
        # Splunk answered. A refusal says nothing about whether it is up.
        assert breaker.state == CircuitState.CLOSED

    async def test_an_unavailable_splunk_is_retried_and_is_a_failed_check(self):
        backend = Backend(Unavailable("Splunk answered HTTP 503"))
        run = executor(backend, retry_max_attempts=3)
        summary = await run.execute_sop(INCIDENT, sop("index=batch_logs ERROR"))
        assert backend.searches == 3
        first = summary.step_results[0]
        assert first.status == StepStatus.FAIL and first.audit["outcome"] == "unavailable"
        # A failed check does follow its failure edge.
        assert [r.step_id for r in summary.step_results][:2] == ["search", "verify"]

    async def test_a_refused_check_inside_verify_is_not_repeated(self):
        backend = Backend()
        steps = sop("index=batch_logs ERROR", verify=True)
        steps.steps[0].parameters["query"] = "index=batch_logs ERROR"
        steps.steps[0].on_success = "verify"
        steps.steps[1].parameters["parameters"] = {"query": "index=batch_logs | outputlookup x"}
        summary = await executor(backend).execute_sop(INCIDENT, steps)
        # The state table has no escalation out of a verification in progress, so this
        # ends as NEEDS_HUMAN. Either way a person has it, and it is not resolved.
        assert summary.outcome == ExecutionOutcome.NEEDS_HUMAN
        assert backend.searches == 1  # the first search only; the refused one never ran
        verify = summary.step_results[-1]
        assert verify.step_id == "verify" and verify.status == StepStatus.ESCALATED

    async def test_a_search_that_passes_carries_its_audit_record(self):
        backend = Backend()
        summary = await executor(backend).execute_sop(INCIDENT, sop("index=batch_logs ERROR"))
        step = summary.step_results[0]
        assert step.status == StepStatus.SUCCESS and step.data["result_count"] == 1
        assert step.audit["spl"] == "search index=batch_logs ERROR | head 100"
        assert (step.audit["earliest"], step.audit["latest"]) == ("-4h", "now")
        assert step.audit["result_count"] == 1 and step.audit["outcome"] == "ok"
        assert "exit code 1" in step.evidence and "exit code 1" not in str(step.audit)


class FlagOnSuccess(BaseAdapter):
    """An adapter that sets the flag on a result that succeeded. The flag must mean nothing."""

    adapter_name = "Odd"

    async def health_check(self) -> bool:
        return True

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        return AdapterResult(success=True, data={"result_count": 0}, escalate=True)


async def test_the_flag_on_a_successful_result_changes_nothing():
    run = SOPExecutor(adapters={"splunk": FlagOnSuccess(), "autosys": MockAutosysAdapter()})
    summary = await run.execute_sop(INCIDENT, sop("index=batch_logs ERROR"))
    assert summary.step_results[0].status == StepStatus.SUCCESS


def test_the_audit_record_survives_the_journal_and_reaches_the_run_record():
    result = StepResult(
        step_id="s", step_type="SPLUNK_SEARCH", status=StepStatus.SUCCESS, audit={"backend": "rest"}
    )
    assert StepResult.from_dict(result.to_dict()) == result
    assert result.to_audit_dict()["audit"] == {"backend": "rest"}
    # A result journalled before the field existed still loads.
    old = {k: v for k, v in result.to_dict().items() if k != "audit"}
    assert StepResult.from_dict(old).audit == {}
