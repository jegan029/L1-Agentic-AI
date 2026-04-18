"""Unit tests for tool definitions."""

from __future__ import annotations

from src.l1_agent.ai.tool_definitions import (
    AUTOSYS_STATUS_TOOL,
    ESCALATE_TOOL,
    FILE_CHECK_TOOL,
    MQ_CHECK_TOOL,
    POST_WORK_NOTE_TOOL,
    RESOLVE_INCIDENT_TOOL,
    SPLUNK_SEARCH_TOOL,
    get_investigation_tools,
    get_sop_selection_tools,
)


class TestToolDefinitions:
    def test_all_tools_have_required_fields(self):
        for tool in get_investigation_tools():
            assert tool["type"] == "function"
            fn = tool["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn

    def test_investigation_tools_count(self):
        tools = get_investigation_tools()
        assert len(tools) == 11  # splunk, mq, file, autosys, dynatrace_vm, dynatrace_metrics, web_ui, mainframe, note, escalate, resolve

    def test_sop_selection_tools_count(self):
        tools = get_sop_selection_tools()
        assert len(tools) == 2  # select_sop, escalate_no_sop

    def test_splunk_tool_has_query_param(self):
        params = SPLUNK_SEARCH_TOOL["function"]["parameters"]
        assert "query" in params["properties"]
        assert "query" in params["required"]

    def test_mq_tool_has_required_params(self):
        params = MQ_CHECK_TOOL["function"]["parameters"]
        assert "queue_manager" in params["properties"]
        assert "queue" in params["properties"]
        assert "action" in params["properties"]

    def test_file_check_tool_has_unc_path(self):
        params = FILE_CHECK_TOOL["function"]["parameters"]
        assert "unc_path" in params["properties"]

    def test_autosys_tool_has_job_name(self):
        params = AUTOSYS_STATUS_TOOL["function"]["parameters"]
        assert "job_name" in params["properties"]

    def test_escalate_tool_has_reason(self):
        params = ESCALATE_TOOL["function"]["parameters"]
        assert "reason" in params["required"]

    def test_resolve_tool_has_summary(self):
        params = RESOLVE_INCIDENT_TOOL["function"]["parameters"]
        assert "resolution_summary" in params["required"]

    def test_post_work_note_tool_has_note(self):
        params = POST_WORK_NOTE_TOOL["function"]["parameters"]
        assert "note" in params["required"]

    def test_sop_selection_tools_have_correct_names(self):
        tools = get_sop_selection_tools()
        names = {t["function"]["name"] for t in tools}
        assert names == {"select_sop", "escalate_no_sop"}
