"""Splunk through the Splunk MCP Server app (streamable HTTP, bearer token).

Only one of the server's tools is ever called: the search tool named in the
connection, and only with a search the guard has passed. The server's own
tool list is read to confirm that tool exists and is never handed on to
anything else.

Every search opens its own MCP session and closes it. Nothing is held between
calls, so a Splunk restart costs the search that was in flight and no more.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import httpx

from src.l1_agent.adapters.splunk.backend import (
    AccessDenied,
    Misconfigured,
    QueryRefused,
    SplunkError,
    Unavailable,
    bearer_token,
    short,
    tls_context,
)
from src.l1_agent.adapters.splunk.guard import GuardedSearch
from src.l1_agent.utils.logging import get_logger

logger = get_logger("splunk_mcp")

DEFAULT_SEARCH_TOOL = "splunk_run_query"
CONNECT_TIMEOUT_SECONDS = 15

# Opens a session: an async context manager yielding something with
# `list_tools()` and `call_tool(name, arguments)`. Tests give a fake.
SessionFactory = Callable[[], Any]


class McpBackend:
    name = "mcp"

    def __init__(
        self,
        url: str,
        token: str,
        search_tool: str = DEFAULT_SEARCH_TOOL,
        *,
        verify_ssl: bool = True,
        ca_bundle: str = "",
        timeout_seconds: int = 120,
        session_factory: Optional[SessionFactory] = None,
    ) -> None:
        self._url = url
        self._token = bearer_token(token)
        self._tool = search_tool or DEFAULT_SEARCH_TOOL
        self._verify_ssl = verify_ssl
        self._ssl = tls_context(verify_ssl, ca_bundle)
        self._timeout = timeout_seconds
        self._open = session_factory or self._sdk_session
        # Whether the server has been seen to offer the tool. Forgotten on any failure.
        self._tool_confirmed = False
        if not verify_ssl:
            logger.warning("Splunk MCP: TLS verification is OFF (demo mode only)")

    @asynccontextmanager
    async def _sdk_session(self) -> AsyncIterator[Any]:
        # Imported here so that the SDK is needed only when this backend is chosen.
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        if not self._verify_ssl:
            logger.warning("Splunk MCP: connecting without TLS verification")
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._token}"},
            verify=self._ssl,
            timeout=httpx.Timeout(self._timeout, connect=CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
        ) as client:
            async with streamable_http_client(self._url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def _confirm_tool(self, session: Any) -> None:
        listed = await session.list_tools()
        if self._tool not in {tool.name for tool in listed.tools}:
            raise Misconfigured(f"the MCP server does not offer the search tool '{self._tool}'")
        self._tool_confirmed = True

    async def _within_timeout(self, work: Callable[[Any], Any]) -> Any:
        """Open a session, do the work, and turn every failure into a backend error."""

        async def run() -> Any:
            async with self._open() as session:
                return await work(session)

        try:
            return await asyncio.wait_for(run(), self._timeout)
        except SplunkError:
            self._tool_confirmed = False
            raise
        except asyncio.TimeoutError:
            self._tool_confirmed = False
            raise Unavailable(f"the search did not finish within {self._timeout}s") from None
        except (Exception, BaseExceptionGroup) as exc:
            self._tool_confirmed = False
            raise self._classify(exc) from None

    def _classify(self, exc: BaseException) -> BaseException:
        leaves = list(_leaves(exc))
        if any(isinstance(leaf, asyncio.CancelledError) for leaf in leaves):
            return asyncio.CancelledError()
        for leaf in leaves:
            if isinstance(leaf, SplunkError):
                return leaf
        for leaf in leaves:
            if isinstance(leaf, httpx.HTTPStatusError):
                status = leaf.response.status_code
                if status in (401, 403):
                    return AccessDenied(f"Splunk refused the credential (HTTP {status})")
                if status >= 500:
                    return Unavailable(f"the MCP server answered HTTP {status}")
                return Misconfigured(f"the MCP endpoint answered HTTP {status}")
        for leaf in leaves:
            if isinstance(leaf, httpx.TimeoutException):
                return Unavailable("the MCP server did not answer in time")
            if isinstance(leaf, (httpx.TransportError, OSError)):
                return Unavailable(f"the MCP server could not be reached ({type(leaf).__name__})")
        name = type(leaves[0]).__name__ if leaves else type(exc).__name__
        return Unavailable(f"the MCP session failed ({name})")

    async def search(self, search: GuardedSearch) -> List[Dict[str, Any]]:
        async def work(session: Any) -> Any:
            if not self._tool_confirmed:
                await self._confirm_tool(session)
            return await session.call_tool(
                self._tool,
                {
                    "query": search.spl,
                    "earliest_time": search.earliest,
                    "latest_time": search.latest,
                    "row_limit": search.max_results,
                },
            )

        return self._rows(await self._within_timeout(work))

    def _rows(self, result: Any) -> List[Dict[str, Any]]:
        text = " ".join(
            str(getattr(block, "text", "") or "")
            for block in (getattr(result, "content", []) or [])
        )
        if getattr(result, "isError", False):
            detail = short(text, self._token)
            if "timed out" in detail.lower() or "timeout" in detail.lower():
                raise Unavailable(f"Splunk reports a timeout: {detail}")
            raise QueryRefused(f"Splunk rejected the search: {detail}")
        payload = getattr(result, "structuredContent", None)
        if not isinstance(payload, dict):
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            # No rows is a result. No readable answer is not.
            raise Unavailable("the MCP server returned no usable result")
        return rows

    async def health(self) -> Tuple[bool, str]:
        try:
            await self._within_timeout(self._confirm_tool)
        except SplunkError as exc:
            return False, short(exc, self._token)
        return True, ""

    async def list_tool_names(self) -> List[str]:
        """For the operator's check script only. Never given to the engine or a model."""

        async def work(session: Any) -> List[str]:
            return sorted(tool.name for tool in (await session.list_tools()).tools)

        return await self._within_timeout(work)

    async def close(self) -> None:
        return None


def _leaves(exc: BaseException) -> Any:
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaves(inner)
    else:
        yield exc
