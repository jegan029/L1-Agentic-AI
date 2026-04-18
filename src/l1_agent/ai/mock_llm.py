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
        super().__init__(LLMSettings(
            endpoint="http://mock-llm",
            api_key="mock-key",
            model="mock-model",
            enabled=False,
        ))
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
        # Count how many tool results we've received
        tool_results = [m for m in messages if m.get("role") == "tool"]
        tool_call_count = len(tool_results)

        # Detect available tool names
        tool_names = set()
        if tools:
            for t in tools:
                fn = t.get("function", {})
                tool_names.add(fn.get("name", ""))

        # Check if this is a SOP selection context
        if "select_sop" in tool_names:
            return self._auto_sop_selection(messages)

        # Investigation phase based on progress
        if tool_call_count == 0:
            # First call: start investigation
            return self._first_investigation_call(messages, tool_names)
        elif tool_call_count < 4:
            # Middle calls: continue investigation
            return self._continue_investigation(messages, tool_names, tool_call_count)
        else:
            # Final call: resolve
            return self._resolve_investigation(messages, tool_results)

    def _auto_sop_selection(
        self, messages: List[Dict[str, Any]]
    ) -> LLMResponse:
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
                    arguments_str=json.dumps({
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
                    }),
                )
            ],
            usage={"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600},
            finish_reason="tool_calls",
            raw={},
        )

    def _first_investigation_call(
        self, messages: List[Dict[str, Any]], tool_names: set
    ) -> LLMResponse:
        """Generate the first investigation tool calls."""
        calls: List[ToolCall] = []

        # Post initial work note
        if "post_work_note" in tool_names:
            calls.append(ToolCall(
                call_id="call_note_1",
                function_name="post_work_note",
                arguments_str=json.dumps({
                    "note": (
                        "Starting AI-driven investigation. Analyzing incident "
                        "description and executing SOP checks. Will check MQ "
                        "queue depth, application logs, and batch job status."
                    ),
                }),
            ))

        # Start with MQ check
        if "mq_check" in tool_names:
            calls.append(ToolCall(
                call_id="call_mq_1",
                function_name="mq_check",
                arguments_str=json.dumps({
                    "queue_manager": "QMPROD01",
                    "queue": "PAYMENT.REQUEST",
                    "action": "depth",
                }),
            ))

        return LLMResponse(
            content="",
            tool_calls=calls,
            usage={"prompt_tokens": 800, "completion_tokens": 150, "total_tokens": 950},
            finish_reason="tool_calls",
            raw={},
        )

    def _continue_investigation(
        self,
        messages: List[Dict[str, Any]],
        tool_names: set,
        call_count: int,
    ) -> LLMResponse:
        """Generate continuation tool calls based on progress."""
        calls: List[ToolCall] = []

        if call_count <= 2:
            # Check MQ status + search Splunk logs
            if "mq_check" in tool_names:
                calls.append(ToolCall(
                    call_id=f"call_mq_{call_count + 1}",
                    function_name="mq_check",
                    arguments_str=json.dumps({
                        "queue_manager": "QMPROD01",
                        "queue": "PAYMENT.REQUEST",
                        "action": "status",
                    }),
                ))
            if "splunk_search" in tool_names:
                calls.append(ToolCall(
                    call_id=f"call_splunk_{call_count + 1}",
                    function_name="splunk_search",
                    arguments_str=json.dumps({
                        "query": (
                            'index=app_logs sourcetype=log4j host=app-server-* '
                            '"PaymentService" (ERROR OR FATAL)'
                        ),
                        "time_range": {"earliest": "-2h", "latest": "now"},
                    }),
                ))
        else:
            # Check Autosys job + post findings
            if "autosys_status" in tool_names:
                calls.append(ToolCall(
                    call_id=f"call_autosys_{call_count + 1}",
                    function_name="autosys_status",
                    arguments_str=json.dumps({
                        "job_name": "BATCH_PAYMENT_PROCESS",
                        "query_type": "status",
                    }),
                ))
            if "post_work_note" in tool_names:
                calls.append(ToolCall(
                    call_id=f"call_note_{call_count + 1}",
                    function_name="post_work_note",
                    arguments_str=json.dumps({
                        "note": (
                            "Investigation progress: MQ queue depth is elevated "
                            "(1523/5000), consumers appear stalled. Application "
                            "logs show connection timeout errors. Checking batch "
                            "job status next."
                        ),
                    }),
                ))

        if not calls:
            return self._resolve_investigation(messages, [])

        return LLMResponse(
            content="",
            tool_calls=calls,
            usage={"prompt_tokens": 1200, "completion_tokens": 200, "total_tokens": 1400},
            finish_reason="tool_calls",
            raw={},
        )

    def _resolve_investigation(
        self,
        messages: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
    ) -> LLMResponse:
        """Generate the resolution tool call."""
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    call_id="call_resolve_final",
                    function_name="resolve_incident",
                    arguments_str=json.dumps({
                        "resolution_summary": (
                            "Investigation complete. Findings:\n"
                            "1. MQ queue PAYMENT.REQUEST on QMPROD01 has elevated "
                            "depth (1523/5000) with no active consumers.\n"
                            "2. Application logs show connection timeout errors to "
                            "MQ broker starting at 09:55.\n"
                            "3. Batch job BATCH_PAYMENT_PROCESS shows FAILURE status "
                            "with exit code 1.\n\n"
                            "Root cause: PaymentService lost connectivity to MQ broker, "
                            "causing message backlog. The batch payment processing job "
                            "has also failed, likely related to the same connectivity issue.\n\n"
                            "All evidence has been collected and attached as work notes."
                        ),
                        "evidence_summary": (
                            "MQ depth: 1523/5000 (consumers stalled), "
                            "App logs: connection timeout errors, "
                            "Autosys: BATCH_PAYMENT_PROCESS FAILURE (exit code 1)"
                        ),
                    }),
                ),
            ],
            usage={"prompt_tokens": 2000, "completion_tokens": 300, "total_tokens": 2300},
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
