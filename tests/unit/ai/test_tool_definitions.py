"""Unit tests for the model-facing tool definitions.

The model is offered exactly two functions, both about choosing an SOP. It is
never offered an operational tool (docs/adr/0001-one-execution-engine.md).
"""

from __future__ import annotations

from src.l1_agent.ai import tool_definitions
from src.l1_agent.ai.tool_definitions import get_sop_selection_tools


class TestToolDefinitions:
    def test_all_tools_have_required_fields(self):
        for tool in get_sop_selection_tools():
            assert tool["type"] == "function"
            fn = tool["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn

    def test_sop_selection_tools_count(self):
        tools = get_sop_selection_tools()
        assert len(tools) == 2  # select_sop, escalate_no_sop

    def test_sop_selection_tools_have_correct_names(self):
        names = {t["function"]["name"] for t in get_sop_selection_tools()}
        assert names == {"select_sop", "escalate_no_sop"}

    def test_no_operational_tools_are_defined_for_the_model(self):
        public = {name for name in vars(tool_definitions) if not name.startswith("_")}
        assert not {n for n in public if n.endswith("_TOOL")}
        assert "get_investigation_tools" not in public
