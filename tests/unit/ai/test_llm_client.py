"""Unit tests for the LLM client."""

from __future__ import annotations

from src.l1_agent.ai.llm_client import LLMClient, LLMResponse, ToolCall
from src.l1_agent.config.settings import LLMSettings


class TestToolCall:
    def test_arguments_property_parses_json(self):
        tc = ToolCall("id1", "my_func", '{"key": "value"}')
        assert tc.arguments == {"key": "value"}

    def test_arguments_property_handles_invalid_json(self):
        tc = ToolCall("id1", "my_func", "not json")
        assert tc.arguments == {}

    def test_arguments_property_handles_none(self):
        tc = ToolCall("id1", "my_func", "")
        assert tc.arguments == {}


class TestLLMResponse:
    def test_from_api_response_with_content(self):
        data = {
            "choices": [
                {
                    "message": {"content": "Hello world", "role": "assistant"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        resp = LLMResponse.from_api_response(data)
        assert resp.content == "Hello world"
        assert resp.tool_calls == []
        assert resp.finish_reason == "stop"
        assert resp.usage["total_tokens"] == 15

    def test_from_api_response_with_tool_calls(self):
        data = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "splunk_search",
                                    "arguments": '{"query": "error"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }
        resp = LLMResponse.from_api_response(data)
        assert resp.content == ""
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].function_name == "splunk_search"
        assert resp.tool_calls[0].arguments == {"query": "error"}
        assert resp.tool_calls[0].call_id == "call_1"

    def test_from_api_response_empty(self):
        resp = LLMResponse.from_api_response({})
        assert resp.content == ""
        assert resp.tool_calls == []

    def test_to_assistant_message_with_content(self):
        resp = LLMResponse(
            content="Some reply",
            tool_calls=[],
            usage={},
            finish_reason="stop",
            raw={},
        )
        msg = resp.to_assistant_message()
        assert msg["role"] == "assistant"
        assert msg["content"] == "Some reply"
        assert "tool_calls" not in msg

    def test_to_assistant_message_with_tool_calls(self):
        resp = LLMResponse(
            content="",
            tool_calls=[ToolCall("c1", "splunk_search", '{"query": "err"}')],
            usage={},
            finish_reason="tool_calls",
            raw={},
        )
        msg = resp.to_assistant_message()
        assert msg["role"] == "assistant"
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "splunk_search"


class TestLLMClientInit:
    def test_client_initializes_with_settings(self):
        settings = LLMSettings(
            endpoint="https://api.example.com/v1",
            api_key="test-key",
            model="gpt-4",
        )
        client = LLMClient(settings)
        assert client._model == "gpt-4"
        assert client._endpoint == "https://api.example.com/v1"
