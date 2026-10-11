"""Model providers (ADR 0009, decision D6).

A provider does one thing: given instructions, a prompt and the schema of one
function, it returns the arguments the model gave for that function, or
nothing. There is no free-text answer and no tool loop. Only the gateway
(`ai/gateway.py`) calls `complete`; `tests/unit/ai/test_model_boundary.py`
searches the source tree for any other caller.

Supported at launch: an OpenAI-compatible endpoint supplied by the customer,
and Anthropic. `DeterministicProvider` is the stand-in used in demo mode, in
CI and by the evaluation suite; it calls nothing.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Protocol

import aiohttp

from src.l1_agent.ai.llm_client import LLMClient, LLMError
from src.l1_agent.config.settings import ConfigError, LLMSettings, Settings
from src.l1_agent.utils.logging import get_logger

logger = get_logger("model_provider")

OPENAI_COMPATIBLE = "openai_compatible"
ANTHROPIC = "anthropic"
DETERMINISTIC = "deterministic"
PROVIDER_KINDS = (OPENAI_COMPATIBLE, ANTHROPIC, DETERMINISTIC)

ANTHROPIC_ENDPOINT = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

KeySource = Callable[[], Awaitable[str]]


class ProviderError(Exception):
    """The provider could not answer. Carries a status or a type, never a body."""


@dataclass(frozen=True)
class ToolSpec:
    """The one function a request asks the model to call."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class Completion:
    # The function arguments, when the model called the function that was asked for.
    arguments: Optional[Dict[str, Any]]
    # What the provider says it counted. None when it did not say.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class Provider(Protocol):
    id: str
    model: str
    region: str
    retention_days: Optional[int]
    input_price_per_mtok: float
    output_price_per_mtok: float

    async def complete(
        self, system: str, user: str, tool: ToolSpec, max_output_tokens: int
    ) -> Completion: ...

    async def close(self) -> None: ...


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class _Declared:
    """What the operator states about an endpoint, from the LLM_* settings."""

    def __init__(self, settings: LLMSettings) -> None:
        self.model = settings.model
        self.region = settings.region
        self.retention_days = settings.retention_days
        self.input_price_per_mtok = settings.input_price_per_mtok
        self.output_price_per_mtok = settings.output_price_per_mtok


class OpenAICompatibleProvider(_Declared):
    """A chat completions endpoint with function calling, named by the customer."""

    id = OPENAI_COMPATIBLE

    def __init__(self, settings: LLMSettings, key: Optional[KeySource] = None) -> None:
        super().__init__(settings)
        self._client = LLMClient(settings, api_key_source=key)

    async def complete(
        self, system: str, user: str, tool: ToolSpec, max_output_tokens: int
    ) -> Completion:
        try:
            response = await self._client.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": dict(tool.parameters),
                        },
                    }
                ],
                # Naming the function is the most widely honoured way to force a call.
                tool_choice={"type": "function", "function": {"name": tool.name}},
                max_tokens=max_output_tokens,
            )
        except LLMError as exc:
            raise ProviderError(str(exc)) from None
        arguments = None
        for call in response.tool_calls:
            if call.function_name == tool.name:
                try:
                    parsed = json.loads(call.arguments_str)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                arguments = parsed if isinstance(parsed, dict) else None
                break
        usage = response.usage if isinstance(response.usage, dict) else {}
        return Completion(
            arguments, _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))
        )

    async def close(self) -> None:
        await self._client.close()


class AnthropicProvider(_Declared):
    """The Anthropic Messages API, with one tool and a forced tool choice.

    Written from the API documentation. It has not run against the live API.
    """

    id = ANTHROPIC

    def __init__(self, settings: LLMSettings, key: Optional[KeySource] = None) -> None:
        super().__init__(settings)
        self._endpoint = (settings.endpoint or ANTHROPIC_ENDPOINT).rstrip("/")
        self._api_key = settings.api_key
        self._key = key
        self._timeout = aiohttp.ClientTimeout(total=settings.timeout_seconds)
        self._session: Optional[aiohttp.ClientSession] = None

    async def complete(
        self, system: str, user: str, tool: ToolSpec, max_output_tokens: int
    ) -> Completion:
        payload = {
            "model": self.model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": dict(tool.parameters),
                }
            ],
            "tool_choice": {"type": "tool", "name": tool.name},
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": await self._key() if self._key else self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        try:
            async with self._session.post(
                f"{self._endpoint}/v1/messages", headers=headers, json=payload
            ) as response:
                if response.status != 200:
                    # The body can echo the prompt; keep the status only.
                    raise ProviderError(f"Anthropic API returned {response.status}")
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ProviderError(f"Anthropic API request failed: {type(exc).__name__}") from None
        arguments = None
        blocks = data.get("content") if isinstance(data, dict) else None
        for block in blocks if isinstance(blocks, list) else []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == tool.name
            ):
                arguments = block["input"] if isinstance(block.get("input"), dict) else None
                break
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        return Completion(
            arguments, _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class DeterministicProvider:
    """The stand-in for a model in demo mode, CI and the evaluation suite.

    It gives the same answer for the same request and reaches no network. Its
    answers are deliberately plain: it ranks by the first SOP id it is shown,
    locates no parameter, and says that it analysed nothing.
    """

    id = DETERMINISTIC
    model = "deterministic-1"
    region = "local"
    retention_days: Optional[int] = 0
    input_price_per_mtok = 0.0
    output_price_per_mtok = 0.0

    async def complete(
        self, system: str, user: str, tool: ToolSpec, max_output_tokens: int
    ) -> Completion:
        arguments = self._answer(user, tool)
        used = len(system) + len(user)
        return Completion(arguments, used // 4, len(json.dumps(arguments)) // 4)

    @staticmethod
    def _answer(user: str, tool: ToolSpec) -> Dict[str, Any]:
        properties = tool.parameters.get("properties", {})
        if tool.name == "select_sop":
            listed = [i for i in properties.get("sop_id", {}).get("enum", []) if i != "NONE"]
            seen = re.search(r'"sop_id": "([^"]+)"', user)
            pick = seen.group(1) if seen and seen.group(1) in listed else "NONE"
            return {
                "sop_id": pick,
                "confidence": 0.9 if pick != "NONE" else 0.0,
                "rationale": "Stand-in for a model: the first candidate listed.",
            }
        if tool.name == "report_parameters":
            return {}
        if tool.name == "report_assessment":
            return {
                "summary": (
                    "Stand-in for a model: no analysis was made. See the result of each "
                    "check above."
                ),
                "likely_cause": "unknown",
                "next_checks": [],
            }
        if tool.name == "draft_sop":
            return {
                "keywords": [],
                "parameters": [],
                "steps": [
                    {
                        "step_id": "review",
                        "type": "NOTE",
                        "description": (
                            "Stand-in for a model: no steps were drafted. Write them from "
                            "the article."
                        ),
                        "on_success": "hand_over",
                    },
                    {
                        "step_id": "hand_over",
                        "type": "ESCALATE",
                        "description": "Hand over to L2.",
                    },
                ],
            }
        return {}

    async def close(self) -> None:
        return None


def build_provider(settings: Settings, key: Optional[KeySource] = None) -> Optional[Provider]:
    """The provider this deployment is configured with, or None when there is none.

    `key` reads the API key from a credential reference (`LLM_API_KEY_REF`) each
    time it is needed. Whether a tenant may use the provider is decided per
    call, by the tenant's model policy.
    """
    llm = settings.llm
    if not llm.enabled:
        return None
    demo = settings.agent.demo_mode
    has_key = bool(llm.api_key or key)
    if llm.provider == DETERMINISTIC or (demo and not has_key):
        if not demo:
            raise ConfigError("LLM_PROVIDER=deterministic is only allowed in demo mode")
        logger.info("Model provider: the deterministic stand-in (demo mode)")
        return DeterministicProvider()
    if llm.provider not in (OPENAI_COMPATIBLE, ANTHROPIC):
        raise ConfigError(f"LLM_PROVIDER must be one of {OPENAI_COMPATIBLE}, {ANTHROPIC}")
    if not has_key:
        logger.warning("LLM_ENABLED is true but no API key is set; the model is off")
        return None
    endpoint = llm.endpoint or (ANTHROPIC_ENDPOINT if llm.provider == ANTHROPIC else "")
    if not endpoint:
        logger.warning("LLM_ENABLED is true but LLM_ENDPOINT is not set; the model is off")
        return None
    if not demo and not endpoint.lower().startswith("https://"):
        raise ConfigError("LLM_ENDPOINT must start with https://")
    if llm.provider == ANTHROPIC:
        if not llm.model or llm.model == LLMSettings.model:
            raise ConfigError("LLM_MODEL must name an Anthropic model when LLM_PROVIDER=anthropic")
        return AnthropicProvider(llm, key)
    return OpenAICompatibleProvider(llm, key)
