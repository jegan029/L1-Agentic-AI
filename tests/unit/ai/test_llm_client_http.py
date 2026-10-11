"""LLMClient against a local OpenAI-compatible fake server (no network, no key)."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.l1_agent.ai.analyzer import AIAnalyzer
from src.l1_agent.ai.llm_client import LLMClient, LLMError
from src.l1_agent.config.settings import LLMSettings
from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP

KEY = "test-key-not-a-real-secret"


def tool_call_response(name: str, arguments: str) -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class FakeProvider:
    def __init__(self, reply, status: int = 200) -> None:
        self.reply = reply
        self.status = status
        self.requests: list = []

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {"headers": dict(request.headers), "body": await request.json(), "path": request.path}
        )
        if self.status != 200:
            return web.json_response(
                {"error": "echo: " + json.dumps(self.requests[-1]["body"])}, status=self.status
            )
        return web.json_response(self.reply)


@pytest.fixture
async def provider_factory():
    servers = []

    async def make(reply, status: int = 200):
        fake = FakeProvider(reply, status)
        app = web.Application()
        app.router.add_post("/v1/chat/completions", fake.handle)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        client = LLMClient(
            LLMSettings(endpoint=str(server.make_url("/v1")), api_key=KEY, model="vendor/model-x")
        )
        return fake, client

    yield make
    for server in servers:
        await server.close()


SOPS = [SOP(sop_id="SOP-MQ-001", title="MQ"), SOP(sop_id="SOP-AUTOSYS-001", title="Autosys")]
INCIDENT = Incident(
    sys_id="s",
    number="INC1",
    short_description="MQ queue depth high",
    description="password=Hunter2! reported by jane.doe@example.com",
)


class TestWireFormat:
    @pytest.mark.asyncio
    async def test_request_shape(self, provider_factory):
        fake, client = await provider_factory(
            tool_call_response(
                "select_sop", '{"sop_id": "SOP-MQ-001", "confidence": 0.9, "rationale": "r"}'
            )
        )
        try:
            result = await AIAnalyzer(client).select_sop(INCIDENT, SOPS)
        finally:
            await client.close()

        sent = fake.requests[0]
        assert sent["path"] == "/v1/chat/completions"
        assert sent["headers"]["Authorization"] == f"Bearer {KEY}"
        assert sent["body"]["model"] == "vendor/model-x"
        assert sent["body"]["tool_choice"] == "required"
        assert {t["function"]["name"] for t in sent["body"]["tools"]} == {
            "select_sop",
            "escalate_no_sop",
        }
        assert result.sop is SOPS[0] and result.confidence == 0.9

    @pytest.mark.asyncio
    async def test_ticket_secrets_and_email_never_leave(self, provider_factory):
        fake, client = await provider_factory(
            tool_call_response("escalate_no_sop", '{"reason": "x"}')
        )
        try:
            await AIAnalyzer(client).select_sop(INCIDENT, SOPS)
        finally:
            await client.close()
        outbound = json.dumps(fake.requests[0]["body"])
        assert "Hunter2!" not in outbound
        assert "jane.doe@example.com" not in outbound
        assert "<<<MQ queue depth high>>>" in outbound


class TestBadProviderBehaviour:
    @pytest.mark.parametrize("status", [401, 404, 429, 500])
    @pytest.mark.asyncio
    async def test_http_error_raises_without_echoing_the_body(self, provider_factory, status):
        _, client = await provider_factory({}, status=status)
        try:
            with pytest.raises(LLMError) as err:
                await client.chat(messages=[{"role": "user", "content": "secret-in-prompt"}])
        finally:
            await client.close()
        assert str(status) in str(err.value)
        assert "secret-in-prompt" not in str(err.value)

    @pytest.mark.parametrize(
        "reply",
        [
            tool_call_response("select_sop", "{not json"),
            tool_call_response("select_sop", '{"sop_id": "SOP-INVENTED", "confidence": 1}'),
            tool_call_response("run_shell", '{"cmd": "rm -rf /"}'),
            {"choices": [{"message": {"role": "assistant", "content": "I pick SOP-MQ-001"}}]},
            {"choices": []},
            {},
        ],
    )
    @pytest.mark.asyncio
    async def test_unusable_output_selects_nothing(self, provider_factory, reply):
        _, client = await provider_factory(reply)
        try:
            result = await AIAnalyzer(client).select_sop(INCIDENT, SOPS)
        finally:
            await client.close()
        assert result.sop is None
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_http_error_selects_nothing(self, provider_factory):
        _, client = await provider_factory({}, status=500)
        try:
            result = await AIAnalyzer(client).select_sop(INCIDENT, SOPS)
        finally:
            await client.close()
        assert result.sop is None
