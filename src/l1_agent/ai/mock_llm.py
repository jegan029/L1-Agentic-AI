"""Mock LLM client for demo mode and testing.

Simulates realistic LLM responses with tool calls so the full AI-driven
workflow can be exercised without a real LLM endpoint.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from src.l1_agent.ai.llm_client import LLMClient, LLMResponse, ToolCall
from src.l1_agent.config.settings import LLMSettings
from src.l1_agent.utils.logging import get_logger

logger = get_logger("mock_llm")


class MockLLMClient(LLMClient):
    """Mock LLM that returns scripted tool-call sequences.

    Used for demo mode and unit testing. The mock simulates the LLM's
    reasoning loop: it issues tool calls for each SOP step, posts work
    notes with analysis, and then resolves or escalates.
    """

    def __init__(self, responses: Optional[List[MockLLMResponse]] = None) -> None:
        # Initialize with dummy settings (no real endpoint needed)
        super().__init__(
            LLMSettings(
                endpoint="http://mock-llm",
                api_key="mock-key",
                model="mock-model",
                enabled=False,
            )
        )
        self._responses = list(responses) if responses else []
        self._call_index = 0
        self._auto_mode = not bool(responses)
        self._conversation_history: List[Dict[str, Any]] = []

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        """Return the next scripted response, or auto-generate one."""
        self._conversation_history = messages

        # If scripted responses are available, use them
        if self._responses and self._call_index < len(self._responses):
            mock_resp = self._responses[self._call_index]
            self._call_index += 1
            logger.info(
                "Mock LLM returning scripted response %d/%d",
                self._call_index,
                len(self._responses),
            )
            return mock_resp.to_llm_response()

        # Auto-generate based on context
        if self._auto_mode:
            return self._auto_generate(messages, tools)

        # Fallback: return empty content (no more tool calls)
        return LLMResponse(
            content="Investigation complete. All available checks have been performed.",
            tool_calls=[],
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            finish_reason="stop",
            raw={},
        )

    async def ask(self, prompt: str) -> str:
        """Return a mock analysis response."""
        return (
            "Based on the incident description, this appears to be a "
            "connectivity/resource issue. Key indicators: queue depth "
            "building up, consumer application not processing messages. "
            "Recommended investigation: Check MQ queue depth and consumer "
            "status, review application logs for connection errors, "
            "verify batch job status."
        )

    async def close(self) -> None:
        pass

    def _auto_generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> LLMResponse:
        """Auto-generate a response based on conversation state.

        Simulates a realistic AI agent that:
        1. First call: analyzes and starts investigation with tool calls
        2. Subsequent calls: interprets results and calls more tools
        3. Final call: resolves or escalates
        """

        # Detect available tool names
        tool_names = set()
        if tools:
            for t in tools:
                fn = t.get("function", {})
                tool_names.add(fn.get("name", ""))

        # Check if this is a SOP selection context
        if "select_sop" in tool_names:
            return self._auto_sop_selection(messages)

        # The model has no operational tools, so there is nothing else to call.
        return LLMResponse(
            content="No tool call is available for this request.",
            tool_calls=[],
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            finish_reason="stop",
            raw={},
        )

    def _auto_sop_selection(self, messages: List[Dict[str, Any]]) -> LLMResponse:
        """Generate SOP selection response."""
        # Extract SOP IDs from the user message
        user_msg = ""
        for m in messages:
            if m.get("role") == "user":
                user_msg = m.get("content", "")

        # Try to find SOP ID in the message
        sop_id = "SOP-MQ-001"  # Default
        if "SOP-" in user_msg:
            import re

            match = re.search(r"SOP-[A-Z]+-\d+", user_msg)
            if match:
                sop_id = match.group(0)

        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    call_id="call_sop_select_1",
                    function_name="select_sop",
                    arguments_str=json.dumps(
                        {
                            "sop_id": sop_id,
                            "confidence": 0.92,
                            "rationale": (
                                "The incident describes MQ queue depth issues with "
                                "messages not being consumed. The SOP covers MQ queue "
                                "depth investigation including queue checks, log analysis, "
                                "and job status verification. The CI (PaymentService) and "
                                "category (Middleware/MQ) match the SOP's applicable "
                                "services and categories. High confidence match."
                            ),
                        }
                    ),
                )
            ],
            usage={"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600},
            finish_reason="tool_calls",
            raw={},
        )


class MockLLMResponse:
    """Helper to define a scripted LLM response for testing."""

    def __init__(
        self,
        content: str = "",
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        finish_reason: str = "stop",
    ) -> None:
        self.content = content
        self.tool_calls_data = tool_calls or []
        self.finish_reason = finish_reason

    def to_llm_response(self) -> LLMResponse:
        calls = [
            ToolCall(
                call_id=tc.get("id", f"call_{i}"),
                function_name=tc.get("function", {}).get("name", ""),
                arguments_str=tc.get("function", {}).get("arguments", "{}"),
            )
            for i, tc in enumerate(self.tool_calls_data)
        ]
        return LLMResponse(
            content=self.content,
            tool_calls=calls,
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            finish_reason=self.finish_reason if not calls else "tool_calls",
            raw={},
        )
