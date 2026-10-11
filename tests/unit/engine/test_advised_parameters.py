"""A parameter value located by a model: what the engine accepts (ADR 0009).

The model points at text in the ticket. It does not supply a value.
"""

from __future__ import annotations

import pytest

from src.l1_agent.engine.parameters import (
    ParameterError,
    accept_advised,
    bind_parameters,
    bind_what_can_be,
    may_be_advised,
    spec_problems,
)
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP, ParameterSpec
from src.l1_agent.sop.validator import validate_sop


def spec(advisor=True, **more) -> ParameterSpec:
    source = {
        "kind": "extraction",
        "fields": ["short_description", "description"],
        "regex": r"queue manager (QM[A-Z0-9_]{1,45})\b",
    }
    if advisor is not None:
        source["advisor"] = advisor
    return ParameterSpec(
        name="queue_manager", source=source, pattern="QM[A-Z0-9_]{1,45}", max_length=48, **more
    )


def ticket(description: str, short: str = "Broker backlog") -> Incident:
    return Incident(sys_id="s", number="INC1", short_description=short, description=description)


class TestAccept:
    def test_a_value_that_stands_in_the_ticket_and_fits_the_pattern_is_accepted(self):
        assert accept_advised(spec(), " QMPROD01 ", ticket("QMPROD01 is the manager")) == "QMPROD01"

    @pytest.mark.parametrize(
        "value, description, why",
        [
            ("QMEVIL01", "QMPROD01 is the manager", "not in the ticket"),
            ("QMPROD", "QMPROD01 is the manager", "part of a longer word"),
            ("PROD01", "QMPROD01 is the manager", "part of a longer word"),
            ("qmprod01", "QMPROD01 is the manager", "not word for word"),
            ("manager", "QMPROD01 is the manager", "in the ticket but not the right shape"),
            ("QMPROD01; rm -rf /", "QMPROD01; rm -rf / is the manager", "breaks the pattern"),
            ("QM" + "A" * 50, "QM" + "A" * 50 + " is the manager", "too long"),
            ("", "QMPROD01", "empty"),
            (None, "QMPROD01", "nothing"),
            (["QMPROD01"], "QMPROD01", "not text"),
            ("[[SECRET_1]]", "the manager is hidden", "a placeholder is not a value"),
        ],
    )
    def test_anything_else_is_refused(self, value, description, why):
        with pytest.raises(ParameterError):
            accept_advised(spec(), value, ticket(description))

    def test_only_the_fields_the_parameter_reads_are_searched(self):
        incident = ticket("nothing here")
        incident.category = "QMPROD01"
        with pytest.raises(ParameterError, match="not in the ticket"):
            accept_advised(spec(), "QMPROD01", incident)

    def test_a_parameter_the_author_did_not_mark_is_never_accepted_from_a_model(self):
        for unmarked in (spec(advisor=False), spec(advisor=None)):
            assert not may_be_advised(unmarked)
            with pytest.raises(ParameterError, match="may not be located by a model"):
                accept_advised(unmarked, "QMPROD01", ticket("QMPROD01"))

    def test_an_enum_still_applies(self):
        with pytest.raises(ParameterError, match="allowed values"):
            accept_advised(spec(enum=["QMTEST01"]), "QMPROD01", ticket("QMPROD01"))


class TestBinding:
    def sop(self, *specs: ParameterSpec) -> SOP:
        return SOP(sop_id="SOP-1", title="t", parameters=list(specs))

    def test_what_binds_is_bound_and_what_was_not_found_is_listed(self):
        constant = ParameterSpec(name="action", source={"kind": "constant", "value": "depth"})
        values, failures, not_found = bind_what_can_be(
            self.sop(constant, spec()), ticket("QMPROD01 is the manager")
        )
        assert values == {"action": "depth"}
        assert list(failures) == ["queue_manager"]
        assert [s.name for s in not_found] == ["queue_manager"]

    def test_a_value_that_was_found_but_is_invalid_is_not_listed_as_not_found(self):
        strict = spec()
        strict.pattern = "QMTEST[0-9]+"
        values, failures, not_found = bind_what_can_be(
            self.sop(strict), ticket("queue manager QMPROD01")
        )
        assert "queue_manager" in failures and not_found == []

    def test_the_strict_binder_is_unchanged(self):
        with pytest.raises(ParameterError, match="could not be found"):
            bind_parameters(self.sop(spec()), ticket("QMPROD01 is the manager"))
        assert bind_parameters(self.sop(spec()), ticket("queue manager QMPROD01")) == {
            "queue_manager": "QMPROD01"
        }


class TestDeclaration:
    def test_advisor_must_be_a_boolean_on_an_extraction_source(self):
        assert spec_problems(spec()) == []
        assert "must be true or false" in " ".join(spec_problems(spec(advisor="yes")))
        constant = ParameterSpec(name="x", source={"kind": "constant", "value": 1, "advisor": True})
        assert "only for an extraction source" in " ".join(spec_problems(constant))

    def test_an_sop_that_makes_a_change_may_not_have_a_model_assisted_parameter(self):
        from tests.actions_support import restart_content

        content = restart_content()
        assert validate_sop(SOP.from_dict(content)) == []
        content["parameters"][0]["source"]["advisor"] = True
        problems = validate_sop(SOP.from_dict(content))
        assert any(
            "ACTION step may not have a parameter with source.advisor" in p for p in problems
        )

    def test_the_marker_survives_the_round_trip_through_the_sop_model(self):
        sop = SOP(sop_id="SOP-1", title="t", parameters=[spec()])
        again = SOP.from_dict(sop.to_dict())
        assert may_be_advised(again.parameters[0])


class TestTheExecutorsOwnGuard:
    """The validator already refuses such an SOP. The executor does not rely on that."""

    class Advisor:
        def __init__(self) -> None:
            self.asked = 0

        async def extract(self, incident, specs):
            self.asked += 1
            return {"job_name": "NIGHTLY_SETTLEMENT_LOAD"}

    async def test_an_sop_that_makes_a_change_never_takes_a_value_from_a_model(self):
        from src.l1_agent.demo import build_mock_adapters
        from src.l1_agent.engine.executor import SOPExecutor
        from tests.actions_support import restart_content

        content = restart_content()
        content["parameters"][0]["source"]["advisor"] = True
        sop = SOP.from_dict(content)
        incident = ticket("Restart NIGHTLY_SETTLEMENT_LOAD please", short="Nightly settlement")
        advisor = self.Advisor()
        with pytest.raises(ParameterError, match="could not be found"):
            await SOPExecutor(adapters=build_mock_adapters())._bind(
                sop, incident, None, advisor, None
            )
        assert advisor.asked == 0, "the model was not even asked"

    async def test_the_same_parameter_in_a_read_only_sop_is_located(self):
        from src.l1_agent.demo import build_mock_adapters
        from src.l1_agent.engine.executor import SOPExecutor
        from tests.actions_support import restart_content

        content = restart_content()
        content["parameters"][0]["source"]["advisor"] = True
        content["steps"] = [s for s in content["steps"] if s["step_type"] != "ACTION"]
        sop = SOP.from_dict(content)
        incident = ticket("Restart NIGHTLY_SETTLEMENT_LOAD please", short="Nightly settlement")
        advisor = self.Advisor()
        values = await SOPExecutor(adapters=build_mock_adapters())._bind(
            sop, incident, None, advisor, None
        )
        assert values == {"job_name": "NIGHTLY_SETTLEMENT_LOAD"} and advisor.asked == 1
