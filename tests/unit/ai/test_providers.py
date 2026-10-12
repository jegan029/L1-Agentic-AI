"""The provider clients and which one a deployment gets (ADR 0009, decision D6).

The Anthropic conversations are in tests/fixtures/contracts/anthropic. They were
written by hand from the API documentation: they show what the client sends and
how it reads each kind of answer, not that the live API answers that way.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.l1_agent.ai.providers import (
    ANTHROPIC,
    DETERMINISTIC,
    OPENAI_COMPATIBLE,
    AnthropicProvider,
    DeterministicProvider,
    OpenAICompatibleProvider,
    ProviderError,
    ToolSpec,
    build_provider,
)
from src.l1_agent.config.settings import AgentSettings, ConfigError, LLMSettings, Settings
from tests.contract.replay import Replay

TOOL = ToolSpec(
    "select_sop",
    "Report the choice.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sop_id": {"type": "string", "enum": ["SOP-1", "NONE"]},
            "rationale": {"type": "string", "maxLength": 600},
        },
        "required": ["sop_id", "rationale"],
    },
)


def settings(llm: LLMSettings, demo: bool = False) -> Settings:
    return Settings(agent=AgentSettings(demo_mode=demo, webhook_secret="s" * 40), llm=llm)


async def anthropic(fixture: str):
    async with Replay.from_file(f"anthropic/{fixture}.json") as server:
        provider = AnthropicProvider(
            LLMSettings(endpoint=server.url, api_key="key-one", model="claude-test-model")
        )
        try:
            return await provider.complete("You choose.", "Which one?", TOOL, 400), server
        finally:
            await provider.close()


class TestAnthropic:
    async def test_one_tool_is_offered_and_the_model_is_made_to_call_it(self):
        completion, server = await anthropic("tool_use")
        server.assert_complete()  # the request matched the documented shape, field for field
        assert completion.arguments == {"sop_id": "SOP-1", "rationale": "It fits."}
        assert (completion.input_tokens, completion.output_tokens) == (321, 17)
        sent = server.requests[0]["json"]
        assert [t["name"] for t in sent["tools"]] == ["select_sop"]
        assert "temperature" not in sent and "stream" not in sent

    async def test_text_around_the_tool_call_is_ignored(self):
        completion, _ = await anthropic("text_before_tool_use")
        assert completion.arguments == {"sop_id": "SOP-1", "rationale": "It fits."}

    @pytest.mark.parametrize(
        "fixture",
        ["text_only", "another_tool", "input_not_an_object", "text_block_with_tool_fields"],
    )
    async def test_anything_but_the_asked_for_tool_call_is_no_answer(self, fixture):
        completion, _ = await anthropic(fixture)
        assert completion.arguments is None, "text that describes a call is never parsed"

    async def test_missing_usage_is_reported_as_unknown_not_zero(self):
        completion, _ = await anthropic("input_not_an_object")
        assert completion.input_tokens is None and completion.output_tokens is None

    @pytest.mark.parametrize("fixture, status", [("overloaded", 529), ("unauthorized", 401)])
    async def test_an_error_carries_the_status_and_nothing_from_the_body(self, fixture, status):
        with pytest.raises(ProviderError) as err:
            await anthropic(fixture)
        assert str(status) in str(err.value)
        assert "Which one" not in str(err.value) and "key-one" not in str(err.value)

    async def test_an_unreachable_endpoint_is_a_provider_error(self):
        provider = AnthropicProvider(
            LLMSettings(endpoint="http://127.0.0.1:9", api_key="k", model="m", timeout_seconds=2)
        )
        try:
            with pytest.raises(ProviderError):
                await provider.complete("s", "u", TOOL, 10)
        finally:
            await provider.close()

    async def test_the_key_is_read_for_each_request_so_a_rotation_takes_effect(self):
        seen = []

        async def handle(request: web.Request) -> web.Response:
            seen.append(request.headers["x-api-key"])
            return web.json_response({"content": [], "usage": {}})

        app = web.Application()
        app.router.add_post("/v1/messages", handle)
        server = TestServer(app)
        await server.start_server()
        keys = iter(["key-before", "key-after"])

        async def key() -> str:
            return next(keys)

        provider = AnthropicProvider(
            LLMSettings(endpoint=str(server.make_url("")).rstrip("/"), model="m"), key
        )
        try:
            await provider.complete("s", "u", TOOL, 10)
            await provider.complete("s", "u", TOOL, 10)
        finally:
            await provider.close()
            await server.close()
        assert seen == ["key-before", "key-after"]


class _OpenAI:
    def __init__(self, reply, status=200):
        self.reply, self.status, self.requests = reply, status, []

    async def __aenter__(self):
        async def handle(request: web.Request) -> web.Response:
            self.requests.append({"headers": dict(request.headers), "body": await request.json()})
            return web.json_response(self.reply, status=self.status)

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handle)
        self._server = TestServer(app)
        await self._server.start_server()
        return self

    async def __aexit__(self, *exc):
        await self._server.close()

    def provider(self, key=None) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            LLMSettings(
                endpoint=str(self._server.make_url("/v1")), api_key="key-one", model="vendor/m"
            ),
            key,
        )


def tool_call(name: str, arguments: str, usage=None) -> dict:
    message = {"tool_calls": [{"id": "c", "function": {"name": name, "arguments": arguments}}]}
    reply = {"choices": [{"finish_reason": "tool_calls", "message": message}]}
    if usage is not None:
        reply["usage"] = usage
    return reply


class TestOpenAICompatible:
    async def test_the_function_is_named_so_the_model_must_call_it(self):
        reply = tool_call(
            "select_sop",
            '{"sop_id": "SOP-1", "rationale": "r"}',
            {"prompt_tokens": 40, "completion_tokens": 6},
        )
        async with _OpenAI(reply) as fake:
            provider = fake.provider()
            completion = await provider.complete("sys", "usr", TOOL, 400)
            await provider.close()
        body = fake.requests[0]["body"]
        assert body["tool_choice"] == {"type": "function", "function": {"name": "select_sop"}}
        assert body["max_tokens"] == 400 and body["model"] == "vendor/m"
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert body["tools"][0]["function"]["parameters"] == dict(TOOL.parameters)
        assert completion.arguments == {"sop_id": "SOP-1", "rationale": "r"}
        assert (completion.input_tokens, completion.output_tokens) == (40, 6)

    @pytest.mark.parametrize(
        "reply",
        [
            tool_call("run_command", '{"cmd": "id"}'),
            tool_call("select_sop", "not json"),
            tool_call("select_sop", '["SOP-1"]'),
            {"choices": [{"message": {"content": 'select_sop({"sop_id": "SOP-1"})'}}]},
            {"choices": []},
            {},
        ],
    )
    async def test_anything_but_the_asked_for_call_is_no_answer(self, reply):
        async with _OpenAI(reply) as fake:
            provider = fake.provider()
            completion = await provider.complete("sys", "usr", TOOL, 100)
            await provider.close()
        assert completion.arguments is None
        assert completion.input_tokens is None, "no usage reported is unknown, not zero"

    async def test_an_error_carries_the_status_only(self):
        async with _OpenAI({"error": "prompt was: usr"}, status=500) as fake:
            provider = fake.provider()
            with pytest.raises(ProviderError) as err:
                await provider.complete("sys", "usr", TOOL, 100)
            await provider.close()
        assert "500" in str(err.value) and "usr" not in str(err.value)

    async def test_a_key_given_by_reference_is_read_for_each_request(self):
        keys = iter(["k1", "k2"])

        async def key() -> str:
            return next(keys)

        async with _OpenAI(tool_call("select_sop", "{}")) as fake:
            provider = fake.provider(key)
            await provider.complete("s", "u", TOOL, 10)
            await provider.complete("s", "u", TOOL, 10)
            await provider.close()
        assert [r["headers"]["Authorization"] for r in fake.requests] == ["Bearer k1", "Bearer k2"]


class TestDeterministic:
    async def test_the_same_request_gets_the_same_answer_and_no_network_is_used(self):
        provider = DeterministicProvider()
        user = 'Candidate SOPs:\n[{"sop_id": "SOP-1"}]'
        first = await provider.complete("s", user, TOOL, 10)
        assert first == await provider.complete("s", user, TOOL, 10)
        assert first.arguments["sop_id"] == "SOP-1"
        assert (await provider.complete("s", "nothing listed", TOOL, 10)).arguments["sop_id"] == (
            "NONE"
        )

    async def test_its_answers_fit_every_task_schema(self):
        from src.l1_agent.ai.advisor import INTERPRET_SCHEMA, draft_schema, extract_schema
        from src.l1_agent.ai.prompts import PROMPTS
        from src.l1_agent.ai.schema import problems
        from src.l1_agent.models.sop import ParameterSpec

        provider = DeterministicProvider()
        for task, schema in (
            ("extract", extract_schema([ParameterSpec(name="x")])),
            ("interpret", INTERPRET_SCHEMA),
            ("draft", draft_schema(["splunk.search"])),
        ):
            tool = ToolSpec(PROMPTS[task].tool, "d", schema)
            answer = (await provider.complete("s", "u", tool, 10)).arguments
            assert problems(schema, answer) == [], task


class TestWhichProviderADeploymentGets:
    def test_none_when_the_model_is_switched_off(self):
        assert build_provider(settings(LLMSettings(enabled=False))) is None

    def test_none_when_there_is_no_key_or_no_endpoint(self):
        assert build_provider(settings(LLMSettings(endpoint="https://m.example/v1"))) is None
        assert build_provider(settings(LLMSettings(api_key="k"))) is None

    def test_demo_mode_without_a_key_gets_the_stand_in(self):
        provider = build_provider(settings(LLMSettings(), demo=True))
        assert provider.id == DETERMINISTIC

    def test_the_stand_in_is_refused_outside_demo_mode(self):
        with pytest.raises(ConfigError, match="demo mode"):
            build_provider(settings(LLMSettings(provider=DETERMINISTIC)))

    def test_an_openai_compatible_endpoint(self):
        llm = LLMSettings(endpoint="https://m.example/v1", api_key="k", model="vendor/m")
        provider = build_provider(settings(llm))
        assert provider.id == OPENAI_COMPATIBLE and provider.model == "vendor/m"

    def test_anthropic_needs_a_model_name_and_defaults_its_endpoint(self):
        with pytest.raises(ConfigError, match="LLM_MODEL"):
            build_provider(settings(LLMSettings(provider=ANTHROPIC, api_key="k")))
        llm = LLMSettings(provider=ANTHROPIC, api_key="k", model="claude-test-model")
        provider = build_provider(settings(llm))
        assert provider.id == ANTHROPIC and provider._endpoint == "https://api.anthropic.com"

    def test_an_unknown_provider_is_a_configuration_error(self):
        with pytest.raises(ConfigError, match="LLM_PROVIDER"):
            build_provider(settings(LLMSettings(provider="bedrock", api_key="k", endpoint="x")))

    def test_plain_http_is_refused_outside_demo_mode(self):
        llm = LLMSettings(endpoint="http://m.example/v1", api_key="k")
        with pytest.raises(ConfigError, match="https"):
            build_provider(settings(llm))
        assert build_provider(settings(llm, demo=True)).id == OPENAI_COMPATIBLE

    def test_a_key_given_only_by_reference_counts_as_a_key(self):
        async def key() -> str:
            return "from-vault"

        llm = LLMSettings(endpoint="https://m.example/v1")
        assert build_provider(settings(llm), key).id == OPENAI_COMPATIBLE

    def test_what_the_operator_declares_is_carried_by_the_provider(self):
        llm = LLMSettings(
            endpoint="https://m.example/v1",
            api_key="k",
            region="eu",
            retention_days=0,
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
        )
        provider = build_provider(settings(llm))
        assert (provider.region, provider.retention_days) == ("eu", 0)
        assert (provider.input_price_per_mtok, provider.output_price_per_mtok) == (3.0, 15.0)

    def test_settings_read_the_declarations_from_the_environment(self, monkeypatch):
        for name, value in {
            "LLM_PROVIDER": "Anthropic",
            "LLM_REGION": "EU",
            "LLM_RETENTION_DAYS": "30",
            "LLM_INPUT_PRICE_PER_MTOK": "3",
            "LLM_OUTPUT_PRICE_PER_MTOK": "15",
            "AGENT_DEMO_MODE": "true",
        }.items():
            monkeypatch.setenv(name, value)
        llm = Settings.from_env().llm
        assert (llm.provider, llm.region, llm.retention_days) == ("anthropic", "eu", 30)
        assert (llm.input_price_per_mtok, llm.output_price_per_mtok) == (3.0, 15.0)
        monkeypatch.delenv("LLM_RETENTION_DAYS")
        assert Settings.from_env().llm.retention_days is None, "not stated is not zero"


def test_the_fixture_note_says_they_are_hand_written():
    from tests.contract.replay import load

    for name in ("tool_use", "overloaded"):
        assert "not captured from the live API" in json.dumps(load(f"anthropic/{name}.json"))
