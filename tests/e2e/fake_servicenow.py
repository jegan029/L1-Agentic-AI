"""A stand-in ServiceNow instance, served over HTTPS on loopback.

It answers the Table API calls the connector makes for intake: listing new
incidents, reading one, updating one, and looking up the integration user.
It was written by hand from the Table API documentation. It is not a
ServiceNow instance and shows nothing about how a real one behaves: no
access controls, no business rules, no data policies.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
import ssl
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

AGENT_SYS_ID = "a" * 32


def self_signed(directory: Path) -> ssl.SSLContext:
    """A certificate for 127.0.0.1 that nothing trusts. The caller turns verification off."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = directory / "snow-cert.pem", directory / "snow-key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_file), str(key_file))
    return context


class FakeServiceNow:
    def __init__(self, directory: Path, user: str = "agent", password: str = "pw") -> None:
        self._directory = directory
        self._authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.tables: Dict[str, Dict[str, Dict[str, Any]]] = {
            "incident": {},
            "sys_user": {AGENT_SYS_ID: {"sys_id": AGENT_SYS_ID, "user_name": user}},
        }
        self.work_notes: Dict[str, List[str]] = {}
        self.refused = 0  # requests without the right credentials
        self.writes: List[Dict[str, Any]] = []
        self._server: TestServer

    async def start(self) -> "FakeServiceNow":
        app = web.Application()
        app.router.add_get("/api/now/table/{table}", self._list)
        app.router.add_get("/api/now/table/{table}/{sys_id}", self._read)
        app.router.add_patch("/api/now/table/{table}/{sys_id}", self._update)
        self._server = TestServer(app, scheme="https")
        await self._server.start_server(ssl=self_signed(self._directory))
        return self

    async def close(self) -> None:
        await self._server.close()

    @property
    def url(self) -> str:
        return str(self._server.make_url("")).rstrip("/")

    def add_incident(self, record: Dict[str, Any]) -> None:
        self.tables["incident"][record["sys_id"]] = {"assigned_to": "", "active": "true", **record}

    # ── Handlers ──────────────────────────────────────────────────────

    def _allowed(self, request: web.Request) -> bool:
        if request.headers.get("Authorization") == self._authorization:
            return True
        self.refused += 1
        return False

    @staticmethod
    def _denied() -> web.Response:
        return web.json_response(
            {"error": {"message": "User Not Authenticated", "detail": "Required to provide Auth"}},
            status=401,
        )

    @staticmethod
    def _shape(record: Dict[str, Any], query: Any) -> Dict[str, Any]:
        wanted = [f for f in query.get("sysparm_fields", "").split(",") if f] or list(record)
        if query.get("sysparm_display_value") == "all":
            return {
                f: {"value": record.get(f, ""), "display_value": record.get(f, "")} for f in wanted
            }
        return {f: record.get(f, "") for f in wanted}

    @staticmethod
    def _matches(record: Dict[str, Any], encoded: str) -> bool:
        for term in encoded.split("^"):
            if not term or term.startswith("ORDERBY") or "=" not in term:
                continue
            name, _, value = term.partition("=")
            if str(record.get(name.split(".")[0], "")) != value:
                return False
        return True

    async def _list(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            return self._denied()
        rows = self.tables.get(request.match_info["table"])
        if rows is None:
            return web.json_response({"error": {"message": "Invalid table"}}, status=400)
        query = request.query
        found = [r for r in rows.values() if self._matches(r, query.get("sysparm_query", ""))]
        offset = int(query.get("sysparm_offset", "0"))
        page = found[offset : offset + int(query.get("sysparm_limit", "10000"))]
        return web.json_response(
            {"result": [self._shape(r, query) for r in page]},
            headers={"X-Total-Count": str(len(found))},
        )

    async def _read(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            return self._denied()
        record = self.tables.get(request.match_info["table"], {}).get(request.match_info["sys_id"])
        if record is None:
            return web.json_response({"error": {"message": "No Record found"}}, status=404)
        return web.json_response({"result": self._shape(record, request.query)})

    async def _update(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            return self._denied()
        sys_id = request.match_info["sys_id"]
        record = self.tables.get(request.match_info["table"], {}).get(sys_id)
        if record is None:
            return web.json_response({"error": {"message": "No Record found"}}, status=404)
        fields = await request.json()
        self.writes.append({"sys_id": sys_id, **fields})
        note = fields.pop("work_notes", None)
        if note:
            self.work_notes.setdefault(sys_id, []).append(str(note))
        record.update(fields)
        return web.json_response({"result": record})
