"""LLM client: handles communication with the AI endpoint.

Supports OpenAI-compatible chat/completions API with function/tool calling.
The API key is loaded from environment variables (secret manager in prod).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import aiohttp

from src.l1_agent.config.settings import LLMSettings
from src.l1_agent.utils.logging import get_logger

logger = get_logger("llm_client")

# Sentinel for fields that must not be logged
_REDACTED = "***REDACTED***"


class LLMClient:
    """Async client for OpenAI-compatible LLM endpoints with tool-calling support."""

    def __init__(self, settings: LLMSettings) -> None:
        self._endpoint = settings.endpoint.rstrip("/")
        self._api_key = settings.api_key
        self._model = settings.model
        self._temperature = settings.temperature
        self._max_tokens = settings.max_tokens
        self._timeout = aiohttp.ClientTimeout(total=settings.timeout_seconds)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Core chat completion ─────────────────────────────────────────

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = "auto",
    ) -> LLMResponse:
        """Send a chat completion request to the LLM endpoint.

        Args:
            messages: Chat messages in OpenAI format.
            tools: Tool/function definitions for function calling.
            tool_choice: "auto", "none", "required", or a dict naming one function.

        Returns:
            LLMResponse with the model's reply and any tool calls.
        """
        url = f"{self._endpoint}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        logger.info(
            "LLM request: model=%s messages=%d tools=%d",
            self._model,
            len(messages),
            len(tools) if tools else 0,
        )

        session = await self._get_session()
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                # The body can echo prompt content; record the status only.
                logger.error("LLM API error %d", resp.status)
                raise LLMError(f"LLM API returned {resp.status}")

            data = await resp.json()

        return LLMResponse.from_api_response(data)

    # ── Convenience: single prompt ───────────────────────────────────

    async def ask(self, prompt: str) -> str:
        """Simple single-turn question without tool calling."""
        resp = await self.chat(
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content


class LLMError(Exception):
    """Raised when the LLM API returns an error."""


class ToolCall:
    """Represents a single tool/function call from the model."""

    def __init__(self, call_id: str, function_name: str, arguments_str: str) -> None:
        self.call_id = call_id
        self.function_name = function_name
        self.arguments_str = arguments_str

    @property
    def arguments(self) -> Dict[str, Any]:
        try:
            return json.loads(self.arguments_str)
        except (json.JSONDecodeError, TypeError):
            return {}


class LLMResponse:
    """Parsed response from an LLM chat completion."""

    def __init__(
        self,
        content: str,
        tool_calls: List[ToolCall],
        usage: Dict[str, int],
        finish_reason: str,
        raw: Dict[str, Any],
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.usage = usage
        self.finish_reason = finish_reason
        self.raw = raw

    @classmethod
    def from_api_response(cls, data: Dict[str, Any]) -> "LLMResponse":
        """Parse an OpenAI-compatible API response."""
        choices = data.get("choices", [{}])
        choice = choices[0] if choices else {}
        message = choice.get("message", {})
        content = message.get("content", "") or ""
        finish_reason = choice.get("finish_reason", "")

        tool_calls: List[ToolCall] = []
        for tc in message.get("tool_calls", []):
            fn = tc.get("function", {})
            tool_calls.append(
                ToolCall(
                    call_id=tc.get("id", ""),
                    function_name=fn.get("name", ""),
                    arguments_str=fn.get("arguments", "{}"),
                )
            )

        usage = data.get("usage", {})
        return cls(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            raw=data,
        )

    def to_assistant_message(self) -> Dict[str, Any]:
        """Convert to an assistant message dict for the conversation history."""
        msg: Dict[str, Any] = {"role": "assistant"}
        if self.content:
            msg["content"] = self.content
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.call_id,
                    "type": "function",
                    "function": {
                        "name": tc.function_name,
                        "arguments": tc.arguments_str,
                    },
                }
                for tc in self.tool_calls
            ]
        return msg
