"""Expression language, typed parameters and the SOP validator."""

from __future__ import annotations

import pytest

from src.l1_agent.engine import expressions
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.expressions import ExpressionError, evaluate
from src.l1_agent.engine.parameters import ParameterError, bind_parameters, render
from src.l1_agent.models.evidence import ExecutionOutcome
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, ParameterSpec
from src.l1_agent.sop.validator import SOPValidationError, ensure_valid, validate_sop

CTX = {
    "result": {
        "current_depth": 12,
        "jobs": [{"status": "SU"}],
        "name": "abc",
        "ok": True,
        "none": None,
    },
    "steps": {"step-2": {"depth": 1523}},
    "all_success": True,
    "any_failed": False,
}


class TestExpressions:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("result.current_depth < 1000", True),
            ("result.current_depth >= 1000", False),
            ('result.jobs[0].status in ["SU", "RU"]', True),
            ('result.jobs[0].status not in ["SU", "RU"]', False),
            ('steps["step-2"].depth > 1000 and all_success', True),
            ("not any_failed", True),
            ("any_failed or result.ok", True),
            ('result.name == "abc"', True),
            ("result.none == null", True),
            ("(result.current_depth < 5) or (result.current_depth == 12)", True),
            ('"b" in result.name', True),
            ("result.current_depth != 12.0", False),
        ],
    )
    def test_evaluates(self, text, expected):
        assert evaluate(text, CTX) is expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "result.missing < 1",  # missing path
            "result.jobs[5].status == 'SU'",  # index out of range
            "result.current_depth",  # not a boolean
            "result.name < 5",  # unlike types
            'result.current_depth == "12"',  # unlike types
            "result.current_depth <",  # malformed
            "result.current_depth < 1000 extra",
            "__import__('os').system('x')",
            "result.current_depth; 1",
            "result.ok and result.current_depth",  # non-boolean operand
            "1 in 5",
            "x" * 600,
        ],
    )
    def test_anything_doubtful_is_an_error_not_a_pass(self, text):
        with pytest.raises(ExpressionError):
            evaluate(text, CTX)

    def test_no_function_calls_or_attribute_tricks(self):
        for text in ["result.__class__", "result.keys()", "open('x')"]:
            with pytest.raises(ExpressionError):
                evaluate(text, CTX)

    def test_root_names(self):
        assert set(expressions.root_names('steps["a"].x > 1 and result.y == 2')) == {
            "steps",
            "result",
        }


def _incident(**kw) -> Incident:
    base = dict(
        sys_id="s",
        number="INC1",
        short_description="Autosys batch job failure - job BATCH_PAYMENT_PROCESS status FA",
        description="details",
        cmdb_ci="ETL-Pipeline",
    )
    base.update(kw)
    return Incident(**base)


def _sop_with(spec: dict, template: str = "${job_name}") -> SOP:
    return SOP.from_dict(
        {
            "sop_id": "P",
            "title": "p",
            "parameters": [spec],
            "steps": [
                {
                    "step_id": "a",
                    "step_type": "AUTOSYS_STATUS",
                    "parameters": {"job_name": template},
                    "on_success": "end",
                },
                {"step_id": "end", "step_type": "ESCALATE"},
            ],
        }
    )


JOB_SPEC = {
    "name": "job_name",
    "source": {"kind": "extraction", "regex": r"\b[Jj]ob\s+([A-Z][A-Z0-9_]{2,63})\b"},
    "pattern": r"[A-Z][A-Z0-9_]{2,63}",
}


class TestParameters:
    def test_extraction_binds_from_ticket_text(self):
        assert bind_parameters(_sop_with(JOB_SPEC), _incident()) == {
            "job_name": "BATCH_PAYMENT_PROCESS"
        }

    def test_unbound_parameter_is_an_error(self):
        with pytest.raises(ParameterError, match="could not be found"):
            bind_parameters(_sop_with(JOB_SPEC), _incident(short_description="something broke"))

    @pytest.mark.parametrize(
        "value",
        ["x; rm -rf /", "a b", "$(whoami)", "job|other", "..\\..\\x", '"quoted"', "a" * 200],
    )
    def test_default_pattern_rejects_unsafe_strings(self, value):
        spec = {"name": "job_name", "source": {"kind": "ticket_field", "field": "cmdb_ci"}}
        with pytest.raises(ParameterError):
            bind_parameters(_sop_with(spec), _incident(cmdb_ci=value))

    def test_enum_and_integer(self):
        spec = ParameterSpec.from_dict(
            {
                "name": "n",
                "type": "integer",
                "source": {"kind": "constant", "value": "7"},
                "enum": [7, 8],
            }
        )
        sop = SOP(sop_id="x", parameters=[spec])
        assert bind_parameters(sop, _incident()) == {"n": 7}
        spec.source = {"kind": "constant", "value": "9"}
        with pytest.raises(ParameterError):
            bind_parameters(sop, _incident())

    def test_render_substitutes_and_keeps_types(self):
        out = render({"a": "${n}", "b": "job ${j}.log", "c": [{"d": "${j}"}]}, {"n": 5, "j": "X"})
        assert out == {"a": 5, "b": "job X.log", "c": [{"d": "X"}]}

    def test_render_refuses_an_unbound_placeholder(self):
        with pytest.raises(ParameterError):
            render({"a": "${nope}"}, {})

    @pytest.mark.asyncio
    async def test_unbound_parameter_stops_the_run_before_any_step(self):
        summary = await SOPExecutor(adapters={}).execute_sop(
            _incident(short_description="nothing useful"), _sop_with(JOB_SPEC)
        )
        assert summary.outcome == ExecutionOutcome.NEEDS_HUMAN
        assert summary.step_results == []
        assert "Parameter binding failed" in summary.escalation_reason

    @pytest.mark.asyncio
    async def test_rendered_value_reaches_the_adapter(self):
        from src.l1_agent.adapters.mock_adapters import MockAutosysAdapter

        summary = await SOPExecutor(adapters={"autosys": MockAutosysAdapter()}).execute_sop(
            _incident(), _sop_with(JOB_SPEC)
        )
        assert "BATCH_PAYMENT_PROCESS" in summary.step_results[0].input_summary
        assert "${" not in summary.step_results[0].input_summary


def _valid() -> dict:
    return {
        "sop_id": "V",
        "title": "v",
        "steps": [
            {
                "step_id": "c",
                "step_type": "SPLUNK_SEARCH",
                "parameters": {"query": "q"},
                "on_success": "v",
                "on_failure": "esc",
            },
            {
                "step_id": "v",
                "step_type": "VERIFY",
                "parameters": {
                    "check": "SPLUNK_SEARCH",
                    "parameters": {"query": "q"},
                    "assert": "result.result_count == 0",
                },
                "on_success": "n",
                "on_failure": "esc",
            },
            {"step_id": "n", "step_type": "NOTE", "on_success": "r"},
            {"step_id": "r", "step_type": "RESOLVE"},
            {"step_id": "esc", "step_type": "ESCALATE"},
        ],
    }


def _problems(mutate) -> list:
    data = _valid()
    mutate(data)
    return validate_sop(SOP.from_dict(data))


class TestValidator:
    def test_valid_sop(self):
        assert validate_sop(SOP.from_dict(_valid())) == []
        ensure_valid(SOP.from_dict(_valid()))

    def test_no_steps(self):
        assert validate_sop(SOP(sop_id="x")) == ["an SOP needs at least one step"]

    def test_draft_cannot_execute(self):
        assert any("draft" in p for p in _problems(lambda d: d.update(status="draft")))

    def test_resolve_reached_from_a_check_is_rejected(self):
        def m(d):
            d["steps"][0]["on_failure"] = "r"

        assert any("RESOLVE can be reached without" in p for p in _problems(m))

    def test_resolve_reached_from_verify_failure_is_rejected(self):
        def m(d):
            d["steps"][1]["on_failure"] = "r"

        assert any("RESOLVE can be reached without" in p for p in _problems(m))

    def test_check_between_verify_and_resolve_is_rejected(self):
        def m(d):
            d["steps"][2] = {
                "step_id": "n",
                "step_type": "SPLUNK_SEARCH",
                "parameters": {"query": "q"},
                "on_success": "r",
            }

        assert any("RESOLVE can be reached without" in p for p in _problems(m))

    def test_resolve_as_first_step_is_rejected(self):
        sop = SOP.from_dict({"sop_id": "x", "steps": [{"step_id": "r", "step_type": "RESOLVE"}]})
        assert any("RESOLVE can be reached without" in p for p in validate_sop(sop))

    def test_dangling_reference(self):
        def m(d):
            d["steps"][0]["on_success"] = "nowhere"

        assert any("unknown step 'nowhere'" in p for p in _problems(m))

    def test_step_must_lead_somewhere(self):
        def m(d):
            d["steps"][2]["on_success"] = ""

        assert any("on_success is required" in p for p in _problems(m))

    def test_cycle(self):
        def m(d):
            d["steps"][2]["on_success"] = "c"

        assert any("cycle" in p for p in _problems(m))

    def test_unknown_step_type_and_rule(self):
        def m(d):
            d["steps"][0] = {
                "step_id": "c",
                "step_type": "DECISION",
                "parameters": {"rule": "depth > 1000"},
                "on_success": "v",
            }

        assert any("unknown name 'depth'" in p for p in _problems(m))

        def m2(d):
            d["steps"][0]["step_type"] = "SHELL"

        assert any("unknown step type" in p for p in _problems(m2))

    def test_adapter_parameters_are_closed(self):
        def m(d):
            d["steps"][0] = {
                "step_id": "c",
                "step_type": "MAINFRAME_CHECK",
                "on_success": "v",
                "parameters": {
                    "action": "screen_check",
                    "screen_navigation": [{"input": "LOGOFF"}],
                },
            }

        assert any("'screen_navigation' is not allowed" in p for p in _problems(m))

    def test_undeclared_placeholder(self):
        def m(d):
            d["steps"][0]["parameters"]["query"] = "index=x ${job_name}"

        assert any("no parameter declares it" in p for p in _problems(m))

    def test_verify_needs_check_and_assert(self):
        def m(d):
            d["steps"][1]["parameters"] = {"check": "NOTE", "assert": "result.x =="}

        problems = _problems(m)
        assert any("'check' must be an adapter step type" in p for p in problems)
        assert any("assert" in p for p in problems)

    def test_terminal_steps_have_no_edges(self):
        def m(d):
            d["steps"][4]["on_success"] = "c"

        assert any("cannot have edges" in p for p in _problems(m))

    def test_ensure_valid_raises_with_all_problems(self):
        with pytest.raises(SOPValidationError) as err:
            ensure_valid(SOP(sop_id="x"))
        assert err.value.problems
