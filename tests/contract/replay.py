"""Replay a recorded HTTP conversation to a connector.

A fixture is a JSON file: an ordered list of exchanges, each the request the
connector is expected to send and the response the target system gives. The
server answers them in order and records anything that does not match, so a
test fails when the connector asks for something the fixture does not show.

The fixtures in tests/fixtures/contracts were written by hand from the
vendors' API documentation. None of them was captured from a live system.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web
from aiohttp.test_utils import TestServer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts"


def load(name: str) -> Any:
    path = FIXTURES / name
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


class Replay:
    def __init__(self, exchanges: List[Dict[str, Any]]) -> None:
        self._exchanges = list(exchanges)
        self._next = 0
        self.problems: List[str] = []
        self.requests: List[Dict[str, Any]] = []
        self._server: TestServer

    @classmethod
    def from_file(cls, name: str) -> "Replay":
        return cls(load(name)["exchanges"])

    async def __aenter__(self) -> "Replay":
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._server = TestServer(app)
        await self._server.start_server()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._server.close()

    @property
    def url(self) -> str:
        return str(self._server.make_url("")).rstrip("/")

    def assert_complete(self) -> None:
        """Every recorded exchange happened, in order, and nothing else did."""
        assert not self.problems, "\n".join(self.problems)
        assert self._next == len(self._exchanges), (
            f"{len(self._exchanges) - self._next} recorded exchange(s) were never requested"
        )

    async def _handle(self, request: web.Request) -> web.Response:
        form = dict(await request.post()) if request.method == "POST" else {}
        body: Any = None
        if request.can_read_body and not form:
            raw = await request.text()
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = raw
        seen = {
            "method": request.method,
            "path": request.path,
            "query": dict(request.query),
            "headers": dict(request.headers),
            "form": form,
            "json": body,
        }
        self.requests.append(seen)
        if self._next >= len(self._exchanges):
            self.problems.append(f"unexpected request {request.method} {request.path_qs}")
            return web.Response(status=599, text="no exchange recorded for this request")
        exchange = self._exchanges[self._next]
        self._next += 1
        want = exchange["request"]
        label = f"exchange {self._next}"
        if want["method"] != seen["method"] or want["path"] != seen["path"]:
            self.problems.append(
                f"{label}: expected {want['method']} {want['path']}, "
                f"got {seen['method']} {seen['path']}"
            )
        for part in ("query", "headers", "form", "json"):
            for key, value in (want.get(part) or {}).items():
                actual = (seen[part] or {}).get(key)
                if actual != value:
                    self.problems.append(f"{label}: {part} {key!r} is {actual!r}, not {value!r}")
        for key in want.get("absent_query", []):
            if key in seen["query"]:
                self.problems.append(f"{label}: query {key!r} should not be sent")
        response = exchange["response"]
        headers = response.get("headers", {})
        if "json" in response:
            return web.json_response(
                response["json"], status=response.get("status", 200), headers=headers
            )
        return web.Response(
            status=response.get("status", 200),
            text=response.get("text", ""),
            content_type=response.get("content_type", "text/html"),
            headers=headers,
        )
