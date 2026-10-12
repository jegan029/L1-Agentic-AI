"""The two Splunk backends against fakes: one result shape, one mapping of errors.

No network. The REST backend's HTTP conversation is in tests/contract; here
its place is taken by a backend that returns rows or raises.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

from src.l1_agent.adapters.splunk.adapter import SplunkSearchAdapter
from src.l1_agent.adapters.splunk.backend import (
    AccessDenied,
    Misconfigured,
    QueryRefused,
    Unavailable,
    bearer_token,
    clean_rows,
    evidence_snippet,
    result_data,
    short,
    tls_context,
)
from src.l1_agent.adapters.splunk.guard import GuardConfig, GuardedSearch
from src.l1_agent.adapters.splunk.mcp_backend import McpBackend
from src.l1_agent.adapters.splunk.rest_backend import RestBackend
from src.l1_agent.config.settings import ConfigError

GUARD = GuardConfig(allowed_indexes=("app_logs", "batch_logs"))
SEARCH = GuardedSearch("search index=app_logs ERROR | head 100", "-1h", "now", 100, ("app_logs",))
TOKEN = "tok-" + "s3cret" * 4

# One event as each backend returns it (shapes seen from Splunk 10 through both paths).
EVENT = {
    "_bkt": "app_logs~0~B983",
    "_cd": "0:19",
    "_indextime": "1791822811",
    "_raw": "   2026-10-12 09:17:05 ERROR [PaymentService] MQ consumer stopped",
    "_serial": "0",
    "_si": ["splunk", "app_logs"],
    "_sourcetype": "log4j",
    "_time": "2026-10-12T09:17:05.000+00:00",
    "host": "app-server-01",
    "index": "app_logs",
    "linecount": "1",
    "source": "/var/log/payment-service/app.log",
    "sourcetype": "log4j",
    "splunk_server": "splunk",
}


def events(n: int) -> List[Dict[str, Any]]:
    return [{**EVENT, "_raw": f"{EVENT['_raw']} #{i}"} for i in range(n)]


class TestNormalisation:
    def test_internal_fields_are_dropped_and_values_are_text(self):
        (row,) = clean_rows([EVENT], 10)
        assert set(row) == {
            "_raw", "_time", "host", "index", "linecount", "source", "sourcetype", "splunk_server",
        }  # fmt: skip
        assert row["_raw"].startswith("2026-10-12 09:17:05 ERROR")
        assert all(isinstance(v, str) for v in row.values())

    def test_values_are_bounded_and_lists_are_joined(self):
        (row,) = clean_rows([{"_raw": "x" * 5000, "tags": ["a", "b"], "n": 7}], 10)
        assert len(row["_raw"]) == 500 and row["tags"] == "a, b" and row["n"] == "7"

    def test_anything_that_is_not_a_row_is_ignored(self):
        assert clean_rows([EVENT, "text", None, 5, ["x"]], 10) == clean_rows([EVENT], 10)
        assert clean_rows(None, 10) == [] and clean_rows({"results": []}, 10) == []

    def test_rows_stop_at_the_limit(self):
        assert len(clean_rows(events(50), 7)) == 7

    def test_the_result_keeps_the_count_and_the_first_ten(self):
        data = result_data(clean_rows(events(37), 100))
        assert data["result_count"] == 37 and len(data["results"]) == 10

    def test_the_evidence_is_at_most_five_lines(self):
        snippet = evidence_snippet(clean_rows(events(37), 100))
        lines = snippet.splitlines()
        assert lines[0] == "Splunk search returned 37 result(s):" and len(lines) == 6
        assert lines[1].startswith("  [1] 2026-10-12 09:17:05 ERROR")
        assert evidence_snippet([]) == "Splunk search returned 0 results."

    def test_a_row_without_raw_text_still_gives_evidence(self):
        assert "count" in evidence_snippet(clean_rows([{"host": "a", "count": "4"}], 10))

    @pytest.mark.parametrize(
        "value", [TOKEN, f"Bearer {TOKEN}", f"bearer  {TOKEN} ", f" {TOKEN}\n"]
    )
    def test_the_token_is_accepted_with_or_without_its_prefix(self, value):
        assert bearer_token(value) == TOKEN

    def test_error_text_is_one_bounded_line_without_the_token(self):
        text = short(f"bad\n  thing {TOKEN} " + "x" * 1000, TOKEN)
        assert TOKEN not in text and "\n" not in text and len(text) <= 300

    def test_tls(self, tmp_path):
        assert tls_context(False) is False
        assert tls_context(True).verify_mode.name == "CERT_REQUIRED"
        with pytest.raises(ConfigError, match="CA bundle"):
            tls_context(True, str(tmp_path / "missing.pem"))


# ── The adapter over a fake backend ───────────────────────────────────


class FakeBackend:
    def __init__(self, name="mcp", rows=None, error=None):
        self.name = name
        self._rows = rows if rows is not None else events(3)
        self._error = error
        self.searches: List[GuardedSearch] = []

    async def search(self, search: GuardedSearch):
        self.searches.append(search)
        if self._error:
            raise self._error
        return self._rows

    async def health(self):
        return (self._error is None, str(self._error or ""))

    async def close(self):
        return None


class TestAdapter:
    async def test_both_backends_give_the_same_result(self):
        params = {"query": "index=app_logs ERROR", "time_range": {"earliest": "-1h"}}
        results = []
        for name in ("mcp", "rest"):
            adapter = SplunkSearchAdapter(FakeBackend(name, events(12)), GUARD)
            results.append(await adapter.execute(params))
            assert adapter.adapter_name == "Splunk"
        mcp, rest = results
        assert mcp.success and rest.success
        assert mcp.data == rest.data and mcp.evidence_snippet == rest.evidence_snippet
        assert mcp.data["result_count"] == 12 and len(mcp.data["results"]) == 10
        assert (mcp.audit["backend"], rest.audit["backend"]) == ("mcp", "rest")
        for key in ("spl", "earliest", "latest", "result_count", "outcome"):
            assert mcp.audit[key] == rest.audit[key]

    async def test_the_backend_is_given_the_guarded_search_only(self):
        backend = FakeBackend()
        await SplunkSearchAdapter(backend, GUARD).execute(
            {"query": "index=app_logs ERROR", "time_range": {"earliest": "-30d"}, "max_results": 5}
        )
        (sent,) = backend.searches
        assert sent.spl == "search index=app_logs ERROR | head 5"
        assert (sent.earliest, sent.latest, sent.max_results) == ("-86400s", "now", 5)

    async def test_the_audit_record_says_what_was_asked_and_not_what_was_found(self):
        result = await SplunkSearchAdapter(FakeBackend(), GUARD).execute(
            {"query": "index=app_logs ERROR", "time_range": {"earliest": "-30d"}}
        )
        assert result.audit == {
            "backend": "mcp",
            "spl": "search index=app_logs ERROR | head 100",
            "earliest": "-86400s",
            "latest": "now",
            "time_range_clamped": True,
            "result_count": 3,
            "duration_ms": result.audit["duration_ms"],
            "outcome": "ok",
        }
        assert "PaymentService" not in str(result.audit)

    async def test_no_rows_is_a_successful_search(self):
        result = await SplunkSearchAdapter(FakeBackend(rows=[]), GUARD).execute(
            {"query": "index=app_logs ERROR"}
        )
        assert result.success and result.data == {"result_count": 0, "results": []}
        assert result.evidence_snippet == "Splunk search returned 0 results."

    @pytest.mark.parametrize(
        "query", ["index=_internal", "index=app_logs | delete", "| rest /services", "", None]
    )
    async def test_a_refused_search_never_reaches_the_backend(self, query):
        backend = FakeBackend()
        result = await SplunkSearchAdapter(backend, GUARD).execute({"query": query})
        assert backend.searches == []
        assert not result.success and result.escalate
        assert result.error.startswith("Search refused by the guard: ")
        assert result.audit["outcome"] == "rejected" and result.audit["spl"] == ""

    @pytest.mark.parametrize(
        "error, outcome, escalate",
        [
            (AccessDenied("Splunk refused the credential (HTTP 401)"), "access_denied", True),
            (AccessDenied("Splunk refused the credential (HTTP 403)"), "access_denied", True),
            (QueryRefused("Splunk rejected the search: bad"), "refused_by_splunk", True),
            (Misconfigured("no such tool"), "misconfigured", True),
            (Unavailable("Splunk answered HTTP 503"), "unavailable", False),
            (Unavailable("the search did not finish within 5s"), "unavailable", False),
            (RuntimeError("boom"), "error", False),
        ],
    )
    async def test_errors_are_mapped_the_same_for_either_backend(self, error, outcome, escalate):
        for name in ("mcp", "rest"):
            result = await SplunkSearchAdapter(FakeBackend(name, error=error), GUARD).execute(
                {"query": "index=app_logs ERROR"}
            )
            assert not result.success and result.data == {}
            assert result.escalate is escalate
            assert result.audit["outcome"] == outcome and result.audit["result_count"] == 0
            assert result.audit["spl"] == "search index=app_logs ERROR | head 100"

    async def test_the_log_has_the_search_and_never_the_events(self, caplog):
        with caplog.at_level(logging.INFO, logger="l1_agent"):
            await SplunkSearchAdapter(FakeBackend(), GUARD).execute(
                {"query": "index=app_logs ERROR"}
            )
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "backend=mcp outcome=ok results=3" in text
        assert "spl=search index=app_logs ERROR | head 100" in text
        assert "PaymentService" not in text and "MQ consumer" not in text

    async def test_health_is_the_backend_s(self):
        assert await SplunkSearchAdapter(FakeBackend(), GUARD).health_check() is True
        broken = SplunkSearchAdapter(FakeBackend(error=Unavailable("down")), GUARD)
        assert await broken.health() == (False, "down")
        assert await broken.health_check() is False


# ── The MCP backend over a fake session ───────────────────────────────


def tool_result(payload=None, *, text=None, is_error=False, structured=True):
    import json

    body = text if text is not None else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=body)],
        structuredContent=payload if structured and text is None else None,
        isError=is_error,
    )


class FakeMcp:
    """Stands in for the MCP server: counts sessions, lists tools, answers one tool."""

    def __init__(self, tools=("splunk_get_info", "splunk_run_query"), answer=None, fail=None):
        self.tools = list(tools)
        self.answer = answer or tool_result({"results": events(2), "total_rows": 2})
        self.fail = list(fail or [])  # errors raised while opening a session, in order
        self.sessions = 0
        self.listed = 0
        self.calls: List[tuple] = []
        self.delay = 0.0

    @asynccontextmanager
    async def open(self):
        self.sessions += 1
        if self.fail:
            error = self.fail.pop(0)
            if error is not None:
                raise error
        yield self

    async def list_tools(self):
        self.listed += 1
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self.tools])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.answer


def mcp_backend(server: FakeMcp, **kwargs) -> McpBackend:
    return McpBackend(
        "https://splunk.example:8089/services/mcp",
        f"Bearer {TOKEN}",
        session_factory=server.open,
        **kwargs,
    )


def http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://splunk.example:8089/services/mcp")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, request=request)
    )


class TestMcpBackend:
    async def test_only_the_search_tool_is_called_with_the_guarded_search(self):
        server = FakeMcp()
        rows = await mcp_backend(server).search(SEARCH)
        assert len(rows) == 2
        assert server.calls == [
            (
                "splunk_run_query",
                {
                    "query": "search index=app_logs ERROR | head 100",
                    "earliest_time": "-1h",
                    "latest_time": "now",
                    "row_limit": 100,
                },
            )
        ]

    async def test_the_tool_is_confirmed_once_and_each_search_has_its_own_session(self):
        server = FakeMcp()
        backend = mcp_backend(server)
        for _ in range(3):
            await backend.search(SEARCH)
        assert server.sessions == 3 and server.listed == 1 and len(server.calls) == 3

    async def test_a_missing_search_tool_is_unhealthy_and_nothing_is_called(self):
        server = FakeMcp(tools=("splunk_get_info",))
        backend = mcp_backend(server)
        ok, reason = await backend.health()
        assert not ok and "does not offer the search tool 'splunk_run_query'" in reason
        with pytest.raises(Misconfigured):
            await backend.search(SEARCH)
        assert server.calls == []

    async def test_another_tool_name_can_be_configured(self):
        server = FakeMcp(tools=("run_splunk_query",))
        await mcp_backend(server, search_tool="run_splunk_query").search(SEARCH)
        assert server.calls[0][0] == "run_splunk_query"

    async def test_health_confirms_the_tool(self):
        assert await mcp_backend(FakeMcp()).health() == (True, "")

    async def test_rows_are_read_from_text_when_there_is_no_structured_content(self):
        answer = tool_result({"results": events(4)}, structured=False)
        answer.structuredContent = None
        assert len(await mcp_backend(FakeMcp(answer=answer)).search(SEARCH)) == 4

    async def test_no_results_is_an_empty_list(self):
        answer = tool_result({"results": [], "truncated": False, "total_rows": 0})
        assert await mcp_backend(FakeMcp(answer=answer)).search(SEARCH) == []

    @pytest.mark.parametrize(
        "answer",
        [
            tool_result(text="not json"),
            tool_result({"rows": []}),
            tool_result({"results": "none"}),
            SimpleNamespace(content=[], structuredContent=None, isError=False),
        ],
    )
    async def test_an_answer_that_is_not_a_result_is_never_an_empty_result(self, answer):
        with pytest.raises(Unavailable, match="no usable result"):
            await mcp_backend(FakeMcp(answer=answer)).search(SEARCH)

    async def test_a_tool_error_is_a_search_splunk_refused(self):
        answer = tool_result(
            text="Error parsing Splunk query: Unknown search command", is_error=True
        )
        with pytest.raises(QueryRefused, match="Unknown search command"):
            await mcp_backend(FakeMcp(answer=answer)).search(SEARCH)

    async def test_a_tool_error_about_a_timeout_may_be_tried_again(self):
        answer = tool_result(text="Search timed out after 60s", is_error=True)
        with pytest.raises(Unavailable):
            await mcp_backend(FakeMcp(answer=answer)).search(SEARCH)

    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_refused_token_is_access_denied(self, status):
        server = FakeMcp(fail=[ExceptionGroup("tg", [http_error(status)])])
        with pytest.raises(AccessDenied, match=str(status)) as caught:
            await mcp_backend(server).search(SEARCH)
        assert TOKEN not in str(caught.value)

    @pytest.mark.parametrize(
        "error",
        [
            http_error(500),
            http_error(503),
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
            ExceptionGroup("tg", [ExceptionGroup("inner", [httpx.ConnectError("refused")])]),
            ConnectionResetError(),
            RuntimeError("stream closed"),
        ],
    )
    async def test_a_server_that_is_down_or_failing_may_be_tried_again(self, error):
        with pytest.raises(Unavailable):
            await mcp_backend(FakeMcp(fail=[error])).search(SEARCH)

    async def test_a_wrong_endpoint_is_a_configuration_problem(self):
        with pytest.raises(Misconfigured, match="404"):
            await mcp_backend(FakeMcp(fail=[http_error(404)])).search(SEARCH)

    async def test_a_search_that_outlives_the_timeout_is_unavailable(self):
        server = FakeMcp()
        server.delay = 5
        with pytest.raises(Unavailable, match="did not finish within"):
            await mcp_backend(server, timeout_seconds=0.05).search(SEARCH)

    async def test_a_splunk_restart_costs_one_search_and_no_more(self):
        server = FakeMcp(fail=[None, httpx.ConnectError("restarting"), None])
        backend = mcp_backend(server)
        await backend.search(SEARCH)
        with pytest.raises(Unavailable):
            await backend.search(SEARCH)
        assert len(await backend.search(SEARCH)) == 2
        # The tool is confirmed again after the failure: the server may have changed.
        assert server.listed == 2

    async def test_cancellation_is_not_swallowed(self):
        server = FakeMcp()
        server.delay = 5
        task = asyncio.ensure_future(mcp_backend(server).search(SEARCH))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_the_tool_list_is_only_for_the_check_script(self):
        backend = mcp_backend(FakeMcp())
        assert await backend.list_tool_names() == ["splunk_get_info", "splunk_run_query"]
        # Nothing the engine can reach carries the server's tools.
        assert not hasattr(SplunkSearchAdapter(backend, GUARD), "list_tool_names")

    def test_tls_off_is_warned_about_when_the_backend_is_built(self, caplog):
        with caplog.at_level(logging.WARNING, logger="l1_agent"):
            mcp_backend(FakeMcp(), verify_ssl=False)
            RestBackend("https://splunk.example:8089", TOKEN, verify_ssl=False)
        messages = [r.getMessage() for r in caplog.records]
        assert sum("TLS verification is OFF" in m for m in messages) == 2
