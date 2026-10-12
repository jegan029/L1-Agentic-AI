"""The answer schemas and the versioned prompts of the advisor tasks (ADR 0009)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.l1_agent.ai.advisor import (
    INTERPRET_SCHEMA,
    draft_schema,
    extract_schema,
    rank_schema,
    sop_content_from_draft,
)
from src.l1_agent.ai.prompts import DATA_RULE, PROMPTS, TASKS
from src.l1_agent.ai.schema import SchemaError, check_schema, problems
from src.l1_agent.models.sop import ParameterSpec

DOCS = Path(__file__).resolve().parents[3] / "docs" / "model-risk"

# A prompt's text cannot change without its version changing: a new digest here
# needs a new version, and an entry in docs/model-risk/change-log.md.
PINNED = {
    "rank": ("rank/2", "230bcd59550bdaacbdaf0b036c8da713fac6abe30886c4530fe4498a833ad070"),
    "extract": ("extract/1", "29998a79f0cee29da9fdac803c970fa0e71ae87877270a86aa633e54f2333387"),
    "interpret": (
        "interpret/1",
        "896550f5bf7e257a08b708655722418cfe7f1018556d19cd54329fc5e66484b7",
    ),
    "draft": ("draft/1", "02e21c4f33b231ec106f681315f9a8ecfd22fcd1bdcb6cd54ec87ef6bcce3b1c"),
}


class TestPrompts:
    def test_there_are_exactly_four_tasks(self):
        assert set(PROMPTS) == set(TASKS) == {"rank", "extract", "interpret", "draft"}

    @pytest.mark.parametrize("task", sorted(PINNED))
    def test_the_text_of_a_version_is_pinned(self, task):
        prompt = PROMPTS[task]
        assert (prompt.version, prompt.digest) == PINNED[task], (
            "the prompt changed: give it a new version and record it in the change log"
        )
        assert prompt.version.startswith(f"{task}/")

    @pytest.mark.parametrize("task", sorted(PINNED))
    def test_every_prompt_says_that_ticket_text_is_data(self, task):
        assert DATA_RULE in PROMPTS[task].system

    def test_the_change_log_and_the_inventory_name_every_prompt_version(self):
        log = (DOCS / "change-log.md").read_text(encoding="utf-8")
        inventory = (DOCS / "model-inventory.md").read_text(encoding="utf-8")
        for prompt in PROMPTS.values():
            assert f"`{prompt.version}`" in log, f"{prompt.version} is not in the change log"
            assert f"`{prompt.version}`" in inventory, f"{prompt.version} is not in the inventory"

    def test_the_inventory_names_every_provider_kind(self):
        from src.l1_agent.ai.providers import PROVIDER_KINDS

        inventory = (DOCS / "model-inventory.md").read_text(encoding="utf-8")
        for kind in PROVIDER_KINDS:
            assert f"`{kind}`" in inventory


SPEC = ParameterSpec(name="queue_manager", max_length=48)
SCHEMAS = {
    "rank": rank_schema(["SOP-1", "SOP-2"]),
    "extract": extract_schema([SPEC]),
    "interpret": INTERPRET_SCHEMA,
    "draft": draft_schema(["splunk.search", "ibm_mq.queue.check"]),
}


class TestTaskSchemas:
    @pytest.mark.parametrize("task", sorted(SCHEMAS))
    def test_every_task_schema_is_closed_and_fully_enforced(self, task):
        check_schema(SCHEMAS[task])  # raises if any part would not be enforced

    def test_rank_accepts_a_candidate_or_none_and_nothing_else(self):
        schema = SCHEMAS["rank"]
        assert problems(schema, {"sop_id": "SOP-1", "rationale": "fits"}) == []
        assert problems(schema, {"sop_id": "NONE", "rationale": ""}) == []
        for bad in (
            {"sop_id": "SOP-9", "rationale": "x"},
            {"sop_id": "SOP-1"},
            {"sop_id": "SOP-1", "rationale": "x", "tool": "autosys.job.force_start"},
            {"sop_id": "SOP-1", "rationale": "x", "confidence": 7},
            {"sop_id": "SOP-1", "rationale": "x", "confidence": float("nan")},
            {"sop_id": "SOP-1", "rationale": "x" * 601},
            {"sop_id": ["SOP-1"], "rationale": "x"},
            "SOP-1",
            None,
        ):
            assert problems(schema, bad), bad

    def test_extract_accepts_only_the_names_that_were_asked_for(self):
        schema = SCHEMAS["extract"]
        assert problems(schema, {}) == []
        assert problems(schema, {"queue_manager": "QMPROD01"}) == []
        assert problems(schema, {"job_name": "X"})
        assert problems(schema, {"queue_manager": 5})
        assert problems(schema, {"queue_manager": "Q" * 49})

    def test_interpret_has_a_bounded_summary_and_a_fixed_list_of_causes(self):
        ok = {"summary": "s", "likely_cause": "unknown", "next_checks": ["a", "b", "c"]}
        assert problems(INTERPRET_SCHEMA, ok) == []
        assert problems(INTERPRET_SCHEMA, {**ok, "likely_cause": "sabotage"})
        assert problems(INTERPRET_SCHEMA, {**ok, "next_checks": ["a"] * 4})
        assert problems(INTERPRET_SCHEMA, {**ok, "summary": "x" * 801})
        assert problems(INTERPRET_SCHEMA, {**ok, "outcome": "resolved"})

    def test_a_draft_cannot_express_an_action_or_an_unlisted_capability(self):
        schema = SCHEMAS["draft"]
        step = {"step_id": "a", "type": "CHECK", "description": "d", "capability": "splunk.search"}
        assert problems(schema, {"steps": [step]}) == []
        assert problems(schema, {"steps": [{**step, "type": "ACTION"}]})
        assert problems(schema, {"steps": [{**step, "capability": "autosys.job.force_start"}]})
        assert problems(schema, {"steps": [{**step, "step_id": "a b; rm"}]})
        assert problems(schema, {"steps": [step] * 31})
        assert problems(schema, {"steps": [step], "change_template": "x"})

    def test_a_draft_becomes_sop_steps_and_bad_arguments_become_none(self):
        content = sop_content_from_draft(
            {
                "keywords": ["k"],
                "parameters": [{"name": "queue", "regex": "queue (\\S+)"}],
                "steps": [
                    {
                        "step_id": "c",
                        "type": "CHECK",
                        "description": "look",
                        "capability": "splunk.search",
                        "arguments": '{"query": "x"}',
                        "on_success": "v",
                    },
                    {
                        "step_id": "v",
                        "type": "VERIFY",
                        "description": "sure",
                        "capability": "ibm_mq.queue.check",
                        "arguments": "not json",
                        "assertion": "result.depth < 5",
                        "on_success": "r",
                    },
                    {"step_id": "r", "type": "RESOLVE", "description": "done"},
                ],
            }
        )
        check, verify, resolve = content["steps"]
        assert check["capability"] == "splunk.search" and check["parameters"] == {"query": "x"}
        assert verify["parameters"] == {
            "check": "ibm_mq.queue.check",
            "parameters": {},
            "assert": "result.depth < 5",
        }
        assert resolve["parameters"] == {"text": "done"}
        assert content["parameters"][0]["source"]["kind"] == "extraction"
        assert "advisor" not in content["parameters"][0]["source"]


class TestValidator:
    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "properties": {}},  # not closed
            {"type": "object", "additionalProperties": True, "properties": {}},
            {"type": "string"},  # unbounded
            {"type": "array", "items": {"type": "string", "maxLength": 5}},  # unbounded
            {"type": "string", "maxLength": 5, "format": "uri"},  # a keyword it would ignore
            {"type": "null"},
            {"anyOf": []},
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": ["missing"],
            },
        ],
    )
    def test_a_schema_it_could_not_enforce_is_refused(self, schema):
        with pytest.raises(SchemaError):
            check_schema(schema)

    def test_types_are_strict(self):
        number = {"type": "number", "minimum": 0, "maximum": 1}
        integer = {"type": "integer", "minimum": 0, "maximum": 9}
        assert problems(number, 0.5) == [] and problems(integer, 3) == []
        for bad in (True, "0.5", None, float("inf"), -1, 2):
            assert problems(number, bad), bad
        assert problems(integer, 1.5) and problems(integer, True)
        assert problems({"type": "boolean"}, 1) and problems({"type": "boolean"}, True) == []
        text = {"type": "string", "maxLength": 5, "pattern": "[a-z]+"}
        assert problems(text, "abc") == [] and problems(text, "abc1") and problems(text, 5)

    def test_a_pattern_must_match_the_whole_value(self):
        text = {"type": "string", "maxLength": 50, "pattern": "[a-z]+"}
        assert problems(text, "abc\n") and problems(text, "abc; drop")
        assert re.fullmatch("[a-z]+", "abc")
