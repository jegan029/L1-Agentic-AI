"""The HTTP client connectors share (ADR 0005).

TLS verification is on and can be switched off only in demo mode. Every call
has a timeout. A response that says the credential was rejected makes the
client read the credential again and try once more, which is how a rotated
credential takes effect without a restart. Reads are retried on the statuses
that mean "try again"; writes are not.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Mapping, Optional, Protocol
from urllib.parse import urlsplit

import aiohttp

from src.l1_agent.config.settings import ConfigError
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets
from src.l1_agent.utils.logging import get_logger

logger = get_logger("connector_http")

RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 30.0
TOKEN_EXPIRY_MARGIN_SECONDS = 30.0


class ConnectorHttpError(RuntimeError):
    """A target system answered with an error. Carries its status and message, never a body."""

    def __init__(self, method: str, path: str, status: int, message: str = "", detail: str = ""):
        self.method = method
        self.path = path
        self.status = status
        self.message = message
        self.detail = detail
        text = f"{method} {path} returned {status}"
        if message:
            text += f": {message}"
        if detail:
            text += f" ({detail})"
        super().__init__(text)


class Auth(Protocol):
    async def headers(self, session: aiohttp.ClientSession) -> Dict[str, str]: ...

    async def rejected(self) -> None:
        """The target refused the credential: forget what is cached."""


class NoAuth:
    async def headers(self, session: aiohttp.ClientSession) -> Dict[str, str]:
        return {}

    async def rejected(self) -> None:
        return None


class BasicAuth:
    def __init__(self, username: str, secrets: ConnectionSecrets, slot: str = "password") -> None:
        self._username = username
        self._secrets = secrets
        self._slot = slot
        self._fresh = False

    async def headers(self, session: aiohttp.ClientSession) -> Dict[str, str]:
        password = await self._secrets.get(self._slot, fresh=self._fresh)
        self._fresh = False
        return {"Authorization": aiohttp.BasicAuth(self._username, password).encode()}

    async def rejected(self) -> None:
        self._fresh = True


class BearerAuth:
    def __init__(self, secrets: ConnectionSecrets, slot: str = "token", scheme: str = "Bearer"):
        self._secrets = secrets
        self._slot = slot
        self._scheme = scheme
        self._fresh = False

    async def headers(self, session: aiohttp.ClientSession) -> Dict[str, str]:
        token = await self._secrets.get(self._slot, fresh=self._fresh)
        self._fresh = False
        return {"Authorization": f"{self._scheme} {token}"}

    async def rejected(self) -> None:
        self._fresh = True


class OAuthClientCredentials:
    """OAuth 2.0 client credentials. With a username, the password grant is used instead.

    The access token is kept in memory until shortly before it expires.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        secrets: ConnectionSecrets,
        *,
        client_secret_slot: str = "client_secret",
        username: str = "",
        password_slot: str = "password",
        scope: str = "",
        verify_tls: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._secrets = secrets
        self._client_secret_slot = client_secret_slot
        self._username = username
        self._password_slot = password_slot
        self._scope = scope
        self._verify_tls = verify_tls
        self._clock = clock
        self._token = ""
        self._expires_at = 0.0
        self._fresh = False
        self._lock = asyncio.Lock()

    async def headers(self, session: aiohttp.ClientSession) -> Dict[str, str]:
        async with self._lock:
            if not self._token or self._clock() >= self._expires_at:
                await self._fetch(session)
        return {"Authorization": f"Bearer {self._token}"}

    async def rejected(self) -> None:
        self._token = ""
        self._fresh = True

    async def _fetch(self, session: aiohttp.ClientSession) -> None:
        fresh, self._fresh = self._fresh, False
        form = {
            "client_id": self._client_id,
            "client_secret": await self._secrets.get(self._client_secret_slot, fresh=fresh),
        }
        if self._username:
            form["grant_type"] = "password"
            form["username"] = self._username
            form["password"] = await self._secrets.get(self._password_slot, fresh=fresh)
        else:
            form["grant_type"] = "client_credentials"
        if self._scope:
            form["scope"] = self._scope
        async with session.post(
            self._token_url, data=form, ssl=None if self._verify_tls else False
        ) as response:
            if response.status >= 400:
                # The body can echo the request; only the status is reported.
                raise ConnectorHttpError("POST", urlsplit(self._token_url).path, response.status)
            body = await response.json(content_type=None)
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise ConnectorHttpError(
                "POST", urlsplit(self._token_url).path, 200, "the token endpoint returned no token"
            )
        self._token = str(token)
        try:
            lifetime = float(body.get("expires_in", 300))
        except (TypeError, ValueError):
            lifetime = 300.0
        self._expires_at = self._clock() + max(lifetime - TOKEN_EXPIRY_MARGIN_SECONDS, 5.0)


class Response:
    def __init__(self, status: int, headers: Mapping[str, str], body: Any) -> None:
        self.status = status
        self.headers = headers
        self.body = body


def check_base_url(url: str, *, demo_mode: bool, name: str) -> str:
    """A target must be reached over TLS. Plain HTTP is for demo mode and its mocks only."""
    cleaned = (url or "").rstrip("/")
    scheme = urlsplit(cleaned).scheme.lower()
    if scheme == "https" or (scheme == "http" and demo_mode):
        return cleaned
    raise ConfigError(f"{name} must start with https://")


class HttpClient:
    def __init__(
        self,
        base_url: str,
        auth: Auth,
        *,
        demo_mode: bool = False,
        verify_tls: bool = True,
        timeout_seconds: float = 30.0,
        retries: int = 2,
        retry_base_delay: float = 0.5,
        name: str = "base_url",
        error_parser: Optional[Callable[[Any], tuple]] = None,
    ) -> None:
        if not verify_tls and not demo_mode:
            raise ConfigError("TLS verification can be switched off only in demo mode")
        self._base_url = check_base_url(base_url, demo_mode=demo_mode, name=name)
        self._auth = auth
        self._verify_tls = verify_tls
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._retries = max(0, retries)
        self._retry_base_delay = retry_base_delay
        self._error_parser = error_parser
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        json: Optional[Mapping[str, Any]] = None,
    ) -> Response:
        """Send a request. Raises ConnectorHttpError for a 4xx or 5xx answer."""
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        # Only a read is safe to send twice after a transient failure.
        attempts = 1 + (self._retries if method.upper() == "GET" else 0)
        credential_retried = False
        attempt = 0
        while True:
            attempt += 1
            headers = {"Accept": "application/json", **await self._auth.headers(session)}
            if json is not None:
                headers["Content-Type"] = "application/json"
            async with session.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                ssl=None if self._verify_tls else False,
                allow_redirects=False,
            ) as raw:
                status = raw.status
                if status < 400:
                    body = await raw.json(content_type=None) if status != 204 else None
                    return Response(status, raw.headers, body)
                message = detail = ""
                try:
                    parsed = await raw.json(content_type=None)
                    if self._error_parser:
                        message, detail = self._error_parser(parsed)
                except Exception:
                    pass  # an HTML error page, for example from a sleeping instance
                retry_after = raw.headers.get("Retry-After", "")
            if status == 401 and not credential_retried:
                # The request was not processed, so this is safe for writes too.
                credential_retried = True
                await self._auth.rejected()
                continue
            if status in RETRY_STATUSES and attempt < attempts:
                await asyncio.sleep(self._delay(attempt, retry_after))
                continue
            raise ConnectorHttpError(method, path, status, message, detail)

    def _delay(self, attempt: int, retry_after: str) -> float:
        try:
            return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
        except (TypeError, ValueError):
            return min(self._retry_base_delay * (2 ** (attempt - 1)), MAX_RETRY_AFTER_SECONDS)
