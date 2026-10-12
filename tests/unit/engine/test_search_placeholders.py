"""A ticket value reaches a Splunk search as a quoted term, checked three times over.

The SOP validator requires the placeholder to stand inside double quotes; the
parameter's pattern limits what the value may contain; and the guard checks
the search as it is after substitution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.l1_agent.adapters.splunk.guard import (
    GuardConfig,
    GuardRejected,
    guard,
    unquoted_placeholders,
)
from src.l1_agent.engine.parameters import ParameterError, bind_parameters, render
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.sop.validator import validate_sop

SAMPLES = Path(__file__).resolve().parents[3] / "data" / "sample_sops"
CONFIG = GuardConfig(allowed_indexes=("batch_logs",))


def autosys_sop(**changes) -> SOP:
    content = json.loads((SAMPLES / "autosys_job_failure.json").read_text(encoding="utf-8"))
    for step in content["steps"]:
        if step["step_type"] == "SPLUNK_SEARCH" and "query" in changes:
            step["parameters"]["query"] = changes["query"]
    if "pattern" in changes:
        content["parameters"][0]["pattern"] = changes["pattern"]
        content["parameters"][0]["source"]["regex"] = changes["regex"]
    return SOP.from_dict(content)


def incident(text: str) -> Incident:
    return Incident(sys_id="i", number="INC1", short_description=text)


class TestWhereAPlaceholderMayStand:
    @pytest.mark.parametrize(
        "query, unquoted",
        [
            ('index=batch_logs "${job_name}" ERROR', []),
            ('index=batch_logs job="${job_name}"', []),
            ('index=batch_logs "job ${job_name} failed"', []),
            ("index=batch_logs ${job_name}", ["job_name"]),
            ("index=batch_logs job=${job_name}", ["job_name"]),
            ("index=${index_name} ERROR", ["index_name"]),
            ('index=batch_logs "a" ${x} "b" ${y}', ["x", "y"]),
            ('index=batch_logs "a \\" ${x} still quoted"', []),
            ('index=batch_logs "a\\\\" ${x}', ["x"]),
            ("index=batch_logs | ${command}", ["command"]),
            (None, []),
        ],
    )
    def test_unquoted_placeholders(self, query, unquoted):
        assert unquoted_placeholders(query) == unquoted

    def test_the_sample_sops_pass(self):
        for path in sorted(SAMPLES.glob("*.json")):
            sop = SOP.from_dict(json.loads(path.read_text(encoding="utf-8")))
            assert validate_sop(sop) == [], path.name

    def test_the_validator_refuses_a_bare_placeholder_in_a_search(self):
        sop = autosys_sop(query="index=batch_logs source=autosys ${job_name} (FAILURE OR ERROR)")
        assert validate_sop(sop) == [
            "step 'step-4': placeholder '${job_name}' must be inside double quotes "
            "in a Splunk search"
        ]

    def test_the_rule_reaches_a_search_inside_a_verify_step(self):
        sop = autosys_sop()
        verify = next(s for s in sop.steps if s.step_type == "VERIFY")
        verify.parameters = {
            "check": "SPLUNK_SEARCH",
            "parameters": {"query": "index=batch_logs ${job_name}"},
            "assert": "result.result_count == 0",
        }
        assert any("must be inside double quotes" in p for p in validate_sop(sop))

    def test_other_connectors_are_not_held_to_it(self):
        # ${job_name} stands bare in the Autosys and file share steps of the same SOP.
        assert validate_sop(autosys_sop()) == []


class TestTheValueItself:
    def test_a_value_from_the_ticket_is_quoted_into_the_search(self):
        sop = autosys_sop()
        values = bind_parameters(sop, incident("Autosys job BATCH_PAYMENT_PROCESS status FA"))
        search = next(s for s in sop.steps if s.step_type == "SPLUNK_SEARCH")
        query = render(search.parameters, values)["query"]
        assert query == (
            'index=batch_logs source=autosys "BATCH_PAYMENT_PROCESS" (FAILURE OR ERROR)'
        )
        assert guard(query, config=CONFIG).indexes == ("batch_logs",)

    @pytest.mark.parametrize(
        "text",
        [
            "nothing here names one",
            'job X" | delete "',
            "job $(whoami)",
            "job A|B",
            "job " + "A" * 200,
        ],
    )
    def test_a_missing_or_invalid_value_stops_the_run_before_any_step(self, text):
        with pytest.raises(ParameterError):
            bind_parameters(autosys_sop(), incident(text))

    def test_a_loose_pattern_cannot_carry_spl_past_the_guard(self):
        """An author's own pattern may admit a quote. The guard still sees the result."""
        sop = autosys_sop(pattern=r'[A-Z"| a-z]+', regex=r'job (X" \| delete ")')
        values = bind_parameters(sop, incident('job X" | delete "'))
        search = next(s for s in sop.steps if s.step_type == "SPLUNK_SEARCH")
        query = render(search.parameters, values)["query"]
        assert '"X" | delete ""' in query
        with pytest.raises(GuardRejected, match="'delete' is not allowed"):
            guard(query, config=CONFIG)
