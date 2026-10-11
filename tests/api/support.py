"""Helpers for HTTP tests: an application on a test database, credentials, a fake identity provider."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from src.l1_agent.api.app import AppContext, create_app
from src.l1_agent.api.security import (
    SESSION_COOKIE,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign_webhook,
)
from src.l1_agent.audit.log import Actor, events_for
from src.l1_agent.auth import store
from src.l1_agent.auth.oidc import OidcClient
from src.l1_agent.config.settings import AgentSettings, AuthSettings, LLMSettings, Settings
from src.l1_agent.connectors.hub import ConnectorHub
from src.l1_agent.connectors.sdk.secrets import SecretResolver
from tests.conftest import Backend, make_service

SECRET = "s" * 40
BASE_URL = "https://agent.test"
ISSUER = "https://idp.test"
CLIENT_ID = "l1-agent"
ADMIN = Actor.system("test-admin")
PAYLOAD = {"sys_id": "abc123", "number": "INC0000001", "short_description": "x"}


def make_settings(agent: Optional[dict] = None, auth: Optional[dict] = None) -> Settings:
    agent_values = dict(demo_mode=False, webhook_secret=SECRET)
    agent_values.update(agent or {})
    return Settings(
        agent=AgentSettings(**agent_values),
        llm=LLMSettings(enabled=False),
        auth=AuthSettings(**(auth or {})),
    )


async def build(
    backend: Backend,
    tenant_id: Optional[uuid.UUID] = None,
    *,
    agent: Optional[dict] = None,
    auth: Optional[dict] = None,
    oidc: Optional[OidcClient] = None,
    saml: Any = None,
    demo_open: bool = False,
    webhook_open: bool = False,
) -> Tuple[FastAPI, AppContext]:
    """The real application, on the test database, for one deployment tenant."""
    tenant_id = tenant_id or await backend.new_tenant()
    settings = make_settings(agent, auth)
    ctx = AppContext(
        settings=settings,
        hub=ConnectorHub(backend.db, settings, SecretResolver({}), refresh_seconds=0),
        db=backend.db,
        runs=make_service(backend.db),
        tenant_id=tenant_id,
        oidc=oidc,
        saml=saml,
        demo_open=demo_open,
        webhook_open=webhook_open,
    )
    return create_app(ctx), ctx


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def token_headers(ctx: AppContext, *permissions: str, tenant_id=None, days: int = 1) -> dict:
    token, _ = await store.create_api_token(
        ctx.db, tenant_id or ctx.tenant_id, "test", list(permissions), days, ADMIN
    )
    return {"Authorization": f"Bearer {token}"}


async def session(ctx: AppContext, roles: List[str], tenant_id=None) -> Tuple[dict, str]:
    """A signed-in user's cookie and CSRF token, created directly in the database."""
    tenant_id = tenant_id or ctx.tenant_id
    async with ctx.db.tenant(tenant_id) as conn:
        user_id = await store.upsert_user(conn, tenant_id, ISSUER, f"user-{uuid.uuid4().hex[:8]}")
        cookie, _ = await store.create_session(conn, tenant_id, user_id, roles, "oidc", hours=1)
    principal = await store.load_session(ctx.db, cookie, idle_minutes=30)
    return {SESSION_COOKIE: cookie}, principal.csrf_token


def signed_headers(body: bytes, secret: str = SECRET, ts: Optional[int] = None) -> dict:
    timestamp = str(int(time.time()) if ts is None else ts)
    return {
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign_webhook(secret, timestamp, body),
        "Content-Type": "application/json",
    }


async def audit_types(ctx: AppContext, tenant_id=None) -> List[str]:
    return [
        e["event_type"] for e in await events_for(ctx.db, tenant_id or ctx.tenant_id, limit=500)
    ]


class FakeIdp:
    """An OpenID Connect provider, served to the client through a mock transport."""

    def __init__(self, issuer: str = ISSUER) -> None:
        self.issuer = issuer
        self.kid = "key-1"
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.published_keys = [self.jwk(self.key, self.kid)]
        self.claims: Dict[str, Any] = {}
        self.id_token: Optional[str] = None  # set to override what the token endpoint returns
        self.token_status = 200
        self.metadata_issuer = issuer
        self.token_requests: List[dict] = []

    @staticmethod
    def jwk(key, kid: str) -> dict:
        data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        return {**data, "kid": kid, "use": "sig", "alg": "RS256"}

    def sign(self, claims: dict, *, key=None, kid=None, algorithm: str = "RS256") -> str:
        return jwt.encode(
            claims, key or self.key, algorithm=algorithm, headers={"kid": kid or self.kid}
        )

    def standard_claims(self, nonce: str, **overrides: Any) -> dict:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "alice",
            "email": "alice@example.com",
            "name": "Alice Example",
            "groups": ["ops"],
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
        }
        claims.update(overrides)
        return {k: v for k, v in claims.items() if v is not None}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.metadata_issuer,
                    "authorization_endpoint": f"{self.issuer}/authorize",
                    "token_endpoint": f"{self.issuer}/token",
                    "jwks_uri": f"{self.issuer}/jwks",
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json={"keys": self.published_keys})
        if path == "/token":
            self.token_requests.append(
                {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            )
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.id_token, "token_type": "Bearer"})
        return httpx.Response(404)

    def client(self, **auth: Any) -> OidcClient:
        settings = AuthSettings(
            oidc_issuer=self.issuer,
            oidc_client_id=CLIENT_ID,
            oidc_client_secret="client-secret-value",
            oidc_redirect_uri=f"{BASE_URL}/auth/callback",
            **auth,
        )
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return OidcClient(settings, http)


async def start_login(http: httpx.AsyncClient) -> Dict[str, str]:
    """GET /auth/login and return the parameters sent to the provider."""
    resp = await http.get("/auth/login")
    assert resp.status_code == 302, resp.text
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}
