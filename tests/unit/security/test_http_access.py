"""Interim access control on the HTTP listener: deny by default on every route."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.l1_agent.api.security import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SecurityConfigError,
    sign_webhook,
    validate_security_config,
)
from src.l1_agent.config.settings import AgentSettings, LLMSettings, Settings
from src.l1_agent.main import L1AgentService

TOKEN = "t" * 40
SECRET = "s" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PAYLOAD = {"sys_id": "abc123", "number": "INC0000001", "short_description": "x"}


def make_settings(**agent) -> Settings:
    base = dict(demo_mode=False, api_tokens=(TOKEN,), webhook_secret=SECRET)
    base.update(agent)
    return Settings(agent=AgentSettings(**base), llm=LLMSettings(enabled=False))


@pytest.fixture
async def service_and_client():
    service = L1AgentService(make_settings())
    spawned = []
    service._spawn = spawned.append  # do not process anything in these tests
    client = TestClient(TestServer(service._create_app()))
    await client.start_server()
    yield service, client, spawned
    await client.close()


def signed_headers(body: bytes, secret: str = SECRET, ts: int | None = None) -> dict:
    timestamp = str(int(time.time()) if ts is None else ts)
    return {
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign_webhook(secret, timestamp, body),
        "Content-Type": "application/json",
    }


class TestEveryRouteNeedsCredentials:
    @pytest.mark.asyncio
    async def test_no_route_is_open_except_liveness(self, service_and_client):
        service, client, _ = service_and_client
        checked = 0
        for route in service._create_app().router.routes():
            if route.method not in ("GET", "POST"):
                continue
            path = route.resource.canonical.replace("{incident_number}", "INC1").replace(
                "{sop_id}", "SOP-1"
            )
            resp = await client.request(route.method, path)
            if path == "/health":
                assert resp.status == 200
            else:
                assert resp.status == 401, f"{route.method} {path} answered {resp.status}"
            checked += 1
        assert checked >= 8

    @pytest.mark.asyncio
    async def test_liveness_reveals_nothing(self, service_and_client):
        _, client, _ = service_and_client
        resp = await client.get("/health")
        assert await resp.json() == {"status": "healthy"}

    @pytest.mark.parametrize(
        "header",
        [
            {},
            {"Authorization": "Bearer wrong" + "x" * 40},
            {"Authorization": f"Basic {TOKEN}"},
            {"Authorization": TOKEN},
            {"Authorization": "Bearer "},
        ],
    )
    @pytest.mark.asyncio
    async def test_bad_tokens_are_refused(self, service_and_client, header):
        _, client, _ = service_and_client
        for path in ("/api/incidents", "/api/dashboard/summary", "/metrics", "/api/stream"):
            resp = await client.get(path, headers=header)
            assert resp.status == 401
            assert await resp.json() == {"error": "unauthorized"}

    @pytest.mark.asyncio
    async def test_valid_token_is_accepted_with_security_headers(self, service_and_client):
        _, client, _ = service_and_client
        for path in ("/api/incidents", "/api/dashboard/summary", "/metrics", "/api/status"):
            resp = await client.get(path, headers=AUTH)
            assert resp.status == 200, path
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert resp.headers["X-Frame-Options"] == "DENY"
            assert "Access-Control-Allow-Origin" not in resp.headers

    @pytest.mark.asyncio
    async def test_cors_is_not_granted_to_an_unlisted_origin(self, service_and_client):
        _, client, _ = service_and_client
        resp = await client.get(
            "/api/incidents", headers={**AUTH, "Origin": "https://evil.example"}
        )
        assert "Access-Control-Allow-Origin" not in resp.headers

    @pytest.mark.asyncio
    async def test_sop_editor_does_not_exist_outside_demo_mode(self, service_and_client):
        _, client, _ = service_and_client
        for method, path in (
            ("GET", "/sop-editor/"),
            ("POST", "/sop-editor/save"),
            ("POST", "/sop-editor/SOP-MQ-001/delete"),
        ):
            resp = await client.request(method, path, headers=AUTH)
            assert resp.status in (404, 405), f"{method} {path} answered {resp.status}"


class TestWebhookSignature:
    @pytest.mark.asyncio
    async def test_valid_signature_is_accepted_once(self, service_and_client):
        _, client, spawned = service_and_client
        body = json.dumps(PAYLOAD).encode()
        headers = signed_headers(body)

        first = await client.post("/webhook/incident", data=body, headers=headers)
        replay = await client.post("/webhook/incident", data=body, headers=headers)

        assert first.status == 200
        assert replay.status == 401
        assert len(spawned) == 1

    @pytest.mark.asyncio
    async def test_bearer_token_is_not_enough_for_the_webhook(self, service_and_client):
        _, client, spawned = service_and_client
        resp = await client.post("/webhook/incident", json=PAYLOAD, headers=AUTH)
        assert resp.status == 401
        assert spawned == []

    @pytest.mark.asyncio
    async def test_tampered_body_wrong_secret_and_stale_timestamp(self, service_and_client):
        _, client, spawned = service_and_client
        body = json.dumps(PAYLOAD).encode()

        tampered = await client.post(
            "/webhook/incident", data=body + b" ", headers=signed_headers(body)
        )
        wrong_secret = await client.post(
            "/webhook/incident", data=body, headers=signed_headers(body, secret="z" * 40)
        )
        stale = await client.post(
            "/webhook/incident",
            data=body,
            headers=signed_headers(body, ts=int(time.time()) - 3600),
        )
        future = await client.post(
            "/webhook/incident",
            data=body,
            headers=signed_headers(body, ts=int(time.time()) + 3600),
        )
        garbage_ts = await client.post(
            "/webhook/incident",
            data=body,
            headers={**signed_headers(body), TIMESTAMP_HEADER: "yesterday"},
        )

        assert [r.status for r in (tampered, wrong_secret, stale, future, garbage_ts)] == [401] * 5
        assert spawned == []

    @pytest.mark.parametrize(
        "payload",
        [
            {"number": "INC1"},  # no sys_id
            {"sys_id": "abc"},  # no number
            {"sys_id": {"value": "x"}, "number": {"oops": 1}},
            {"sys_id": "a b; rm", "number": "INC1"},
            ["not", "an", "object"],
        ],
    )
    @pytest.mark.asyncio
    async def test_signed_but_malformed_payload_is_rejected(self, service_and_client, payload):
        _, client, spawned = service_and_client
        body = json.dumps(payload).encode()
        resp = await client.post("/webhook/incident", data=body, headers=signed_headers(body))
        assert resp.status == 400
        assert spawned == []

    @pytest.mark.asyncio
    async def test_oversized_body_is_rejected(self, service_and_client):
        _, client, spawned = service_and_client
        body = json.dumps({**PAYLOAD, "description": "x" * 400_000}).encode()
        resp = await client.post("/webhook/incident", data=body, headers=signed_headers(body))
        assert resp.status == 413
        assert spawned == []


class TestStartupConfiguration:
    def test_refuses_to_start_without_credentials(self):
        for missing in ({"api_tokens": ()}, {"webhook_secret": ""}):
            with pytest.raises(SecurityConfigError):
                L1AgentService(make_settings(**missing))

    def test_refuses_short_secrets_and_wildcard_cors(self):
        for bad in (
            {"api_tokens": ("short",)},
            {"webhook_secret": "short"},
            {"cors_allowed_origins": ("*",)},
        ):
            with pytest.raises(SecurityConfigError):
                validate_security_config(make_settings(**bad).agent)

    def test_demo_mode_without_credentials_is_loopback_only(self):
        open_demo = AgentSettings(demo_mode=True)
        validate_security_config(open_demo)  # default host is loopback
        with pytest.raises(SecurityConfigError):
            validate_security_config(replace(open_demo, webhook_host="0.0.0.0"))

    def test_default_bind_address_is_loopback(self):
        assert AgentSettings().webhook_host == "127.0.0.1"

    @pytest.mark.asyncio
    async def test_demo_mode_serves_the_editor_with_script_blocked(self):
        service = L1AgentService(
            Settings(agent=AgentSettings(demo_mode=True), llm=LLMSettings(enabled=False))
        )
        client = TestClient(TestServer(service._create_app()))
        await client.start_server()
        try:
            resp = await client.get("/sop-editor/")
            assert resp.status == 200
            assert "script-src" not in resp.headers["Content-Security-Policy"]
            assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
        finally:
            await client.close()
