"""Splunk over its REST search API (port 8089).

For Splunk versions that cannot run the MCP Server app. One HTTP session is
kept for the life of the backend. A search is a job: create it, poll until it
is done, read the results. A job that outlives the timeout is cancelled so it
does not go on using the search head.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from src.l1_agent.adapters.splunk.backend import (
    AccessDenied,
    QueryRefused,
    Unavailable,
    bearer_token,
    short,
    tls_context,
)
from src.l1_agent.adapters.splunk.guard import GuardedSearch
from src.l1_agent.utils.logging import get_logger

logger = get_logger("splunk_rest")

REQUEST_TIMEOUT_SECONDS = 30


class RestBackend:
    name = "rest"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify_ssl: bool = True,
        ca_bundle: str = "",
        timeout_seconds: int = 120,
        poll_interval: float = 2.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = bearer_token(token)
        self._verify_ssl = verify_ssl
        self._ssl = tls_context(verify_ssl, ca_bundle)
        self._timeout = timeout_seconds
        self._poll_interval = poll_interval
        self._session: Optional[aiohttp.ClientSession] = None
        if not verify_ssl:
            logger.warning("Splunk REST: TLS verification is OFF (demo mode only)")

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            )
        return self._session

    async def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        """One call. Every way it can go wrong becomes one of the backend errors."""
        try:
            async with self._http().request(
                method, f"{self._base_url}{path}", ssl=self._ssl, **kwargs
            ) as resp:
                if resp.status in (401, 403):
                    raise AccessDenied(f"Splunk refused the credential (HTTP {resp.status})")
                if resp.status >= 500:
                    raise Unavailable(f"Splunk answered HTTP {resp.status}")
                if resp.status >= 400:
                    detail = short(self._messages(await self._json(resp)), self._token)
                    raise QueryRefused(f"Splunk rejected the request (HTTP {resp.status}) {detail}")
                return await self._json(resp)
        except asyncio.TimeoutError:
            raise Unavailable("Splunk did not answer in time") from None
        except aiohttp.ClientError as exc:
            raise Unavailable(f"Splunk could not be reached ({type(exc).__name__})") from None

    @staticmethod
    async def _json(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
        try:
            body = await resp.json(content_type=None)
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _messages(body: Dict[str, Any]) -> str:
        messages = body.get("messages")
        if not isinstance(messages, list):
            return ""
        return "; ".join(str(m.get("text", "")) for m in messages if isinstance(m, dict))

    async def search(self, search: GuardedSearch) -> List[Dict[str, Any]]:
        if not self._verify_ssl:
            logger.warning("Splunk REST: connecting without TLS verification")
        deadline = time.monotonic() + self._timeout
        created = await self._request(
            "POST",
            "/services/search/jobs",
            data={
                "search": search.spl,
                "output_mode": "json",
                "exec_mode": "normal",
                "earliest_time": search.earliest,
                "latest_time": search.latest,
                "max_count": str(search.max_results),
            },
        )
        sid = str(created.get("sid") or "")
        if not sid:
            raise Unavailable("Splunk created no search job")
        try:
            await self._wait(sid, deadline)
            body = await self._request(
                "GET",
                f"/services/search/jobs/{sid}/results",
                params={"output_mode": "json", "count": str(search.max_results)},
            )
        except (Unavailable, asyncio.CancelledError):
            # Timed out, lost, or abandoned by the caller: do not leave the job running.
            await asyncio.shield(self._cancel(sid))
            raise
        results = body.get("results")
        return results if isinstance(results, list) else []

    async def _wait(self, sid: str, deadline: float) -> None:
        while True:
            body = await self._request(
                "GET", f"/services/search/jobs/{sid}", params={"output_mode": "json"}
            )
            entries = body.get("entry") or [{}]
            content = entries[0].get("content", {}) if isinstance(entries[0], dict) else {}
            if content.get("isFailed"):
                raise QueryRefused("Splunk reports the search job failed")
            if content.get("isDone"):
                return
            if time.monotonic() + self._poll_interval > deadline:
                raise Unavailable(f"the search did not finish within {self._timeout}s")
            await asyncio.sleep(self._poll_interval)

    async def _cancel(self, sid: str) -> None:
        try:
            await self._request(
                "POST", f"/services/search/jobs/{sid}/control", data={"action": "cancel"}
            )
        except Exception as exc:
            logger.warning("Splunk REST: cancelling job failed: %s", type(exc).__name__)

    async def health(self) -> Tuple[bool, str]:
        try:
            await self._request("GET", "/services/server/info", params={"output_mode": "json"})
        except Exception as exc:
            return False, short(exc, self._token)
        return True, ""

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
