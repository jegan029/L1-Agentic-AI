"""Unit tests for the mock LLM client."""

from __future__ import annotations

import json

import pytest

from src.l1_agent.ai.mock_llm import MockLLMClient, MockLLMResponse


class TestMockLLMClient:
    @pytest.mark.asyncio
    async def test_scripted_responses_returned_in_order(self):
        responses = [
            MockLLMResponse(content="First"),
            MockLLMResponse(content="Second"),
        ]
        client = MockLLMClient(responses=responses)

        r1 = await client.chat(messages=[{"role": "user", "content": "hi"}])
        r2 = await client.chat(messages=[{"role": "user", "content": "hi"}])

        assert r1.content == "First"
        assert r2.content == "Second"

    @pytest.mark.asyncio
    async def test_scripted_response_with_tool_calls(self):
        responses = [
            MockLLMResponse(
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {
                            "name": "splunk_search",
                            "arguments": json.dumps({"query": "error"}),
                        },
                    }
                ],
            ),
        ]
        client = MockLLMClient(responses=responses)

        r = await client.chat(
            messages=[{"role": "user", "content": "search"}],
            tools=[{"type": "function", "function": {"name": "splunk_search"}}],
        )

        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].function_name == "splunk_search"

    @pytest.mark.asyncio
    async def test_auto_mode_sop_selection(self):
        client = MockLLMClient()  # auto mode (no scripted responses)

        r = await client.chat(
            messages=[
                {"role": "system", "content": "Select SOP"},
                {"role": "user", "content": "SOP-MQ-001 is available"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "select_sop", "parameters": {}},
                },
            ],
        )

        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].function_name == "select_sop"

    @pytest.mark.asyncio
    async def test_auto_mode_investigation_starts_tools(self):
        client = MockLLMClient()

        r = await client.chat(
            messages=[
                {"role": "system", "content": "Execute SOP"},
                {"role": "user", "content": "Investigate MQ issue"},
            ],
            tools=[
                {"type": "function", "function": {"name": "mq_check"}},
                {"type": "function", "function": {"name": "post_work_note"}},
                {"type": "function", "function": {"name": "resolve_incident"}},
            ],
        )

        assert len(r.tool_calls) >= 1

    @pytest.mark.asyncio
    async def test_ask_returns_analysis(self):
        client = MockLLMClient()
        result = await client.ask("Analyze this incident")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_close_is_noop(self):
        client = MockLLMClient()
        await client.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_exhausted_scripted_responses_returns_stop(self):
        responses = [MockLLMResponse(content="Only one")]
        client = MockLLMClient(responses=responses)

        r1 = await client.chat(messages=[{"role": "user", "content": "1"}])
        r2 = await client.chat(messages=[{"role": "user", "content": "2"}])

        assert r1.content == "Only one"
        assert r2.content != ""  # fallback message
        assert r2.tool_calls == []


class TestMockLLMResponse:
    def test_to_llm_response_with_content(self):
        mock = MockLLMResponse(content="Hello")
        resp = mock.to_llm_response()
        assert resp.content == "Hello"
        assert resp.tool_calls == []

    def test_to_llm_response_with_tool_calls(self):
        mock = MockLLMResponse(
            tool_calls=[
                {
                    "id": "c1",
                    "function": {
                        "name": "mq_check",
                        "arguments": '{"queue": "Q1"}',
                    },
                }
            ],
        )
        resp = mock.to_llm_response()
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].function_name == "mq_check"
        assert resp.finish_reason == "tool_calls"
