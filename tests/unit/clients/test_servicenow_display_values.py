"""Contract tests: incident parsing for each ``sysparm_display_value`` setting.

The fixtures in tests/fixtures/servicenow/ are hand-written from the ServiceNow
Table API documentation. They are not recordings from a live instance, so the
shapes on a customised instance remain unverified.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.config.settings import ServiceNowSettings
from src.l1_agent.models.incident import Incident

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "servicenow"
SYS_ID = "9d385017c611228701d22104cc95c371"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "fixture",
    ["incident_display_all.json", "incident_display_true.json", "incident_display_false.json"],
)
class TestEveryDisplaySetting:
    def test_every_field_is_a_plain_value(self, fixture):
        incident = Incident.from_servicenow(load(fixture)["result"][0])
        for name, value in incident.to_dict().items():
            assert isinstance(value, (str, int)), f"{name} is {type(value).__name__}"
        assert isinstance(incident.sys_id, str)

    def test_identity_and_text(self, fixture):
        incident = Incident.from_servicenow(load(fixture)["result"][0])
        assert incident.number == "INC0010023"
        assert incident.short_description.startswith("Autosys batch job failure")
        assert "BATCH_PAYMENT_PROCESS" in incident.description
        assert incident.correlation_id == "INC0010023"

    def test_sys_id_is_hashable_for_idempotency(self, fixture):
        incident = Incident.from_servicenow(load(fixture)["result"][0])
        assert incident.sys_id in {incident.sys_id}


class TestDisplayAll:
    """The setting the poller uses."""

    def setup_method(self):
        self.incident = Incident.from_servicenow(load("incident_display_all.json")["result"][0])

    def test_sys_id_is_the_raw_value(self):
        assert self.incident.sys_id == SYS_ID

    def test_reference_fields_use_display_names(self):
        assert self.incident.assignment_group == "L1-Batch-Support"
        assert self.incident.cmdb_ci == "ETL-Pipeline"
        assert self.incident.caller == "Monitoring Integration"

    def test_choice_fields(self):
        assert self.incident.category == "Batch"
        assert self.incident.priority == 3
        assert self.incident.state == 1

    def test_timestamp_uses_the_stored_value(self):
        assert self.incident.created_on == "2026-04-14 02:06:11"

    def test_empty_reference_becomes_empty_string(self):
        record = load("incident_display_all.json")["result"][0]
        record["cmdb_ci"] = {"display_value": "", "value": "", "link": ""}
        record["caller_id"] = {"display_value": None, "value": None}
        incident = Incident.from_servicenow(record)
        assert incident.cmdb_ci == "" and incident.caller == ""


class TestDisplayFalse:
    def test_reference_fields_fall_back_to_the_sys_id(self):
        incident = Incident.from_servicenow(load("incident_display_false.json")["result"][0])
        assert incident.sys_id == SYS_ID
        assert incident.assignment_group == "287ebd7da9fe198100f92cc8d1d2154e"
        assert incident.priority == 3


class TestClient:
    @pytest.mark.asyncio
    async def test_poll_returns_usable_incidents(self):
        client = ServiceNowClient(ServiceNowSettings(base_url="https://example.service-now.com"))
        with patch.object(
            client, "_get", AsyncMock(return_value=load("incident_display_all.json"))
        ) as get:
            incidents = await client.get_new_incidents()
        assert [i.sys_id for i in incidents] == [SYS_ID]
        assert get.call_args.args[1]["sysparm_display_value"] == "all"
        # The exact failure from before: membership in the idempotency set.
        assert incidents[0].sys_id not in set()

    @pytest.mark.asyncio
    async def test_single_fetch_asks_for_the_same_shape(self):
        client = ServiceNowClient(ServiceNowSettings(base_url="https://example.service-now.com"))
        record = load("incident_display_all.json")["result"][0]
        with patch.object(client, "_get", AsyncMock(return_value={"result": record})) as get:
            incident = await client.get_incident(SYS_ID)
        assert get.call_args.args[1] == {"sysparm_display_value": "all"}
        assert incident.assignment_group == "L1-Batch-Support"

    def test_none_and_missing_fields(self):
        incident = Incident.from_servicenow({"sys_id": None, "number": None})
        assert incident.sys_id == "" and incident.number == ""


class TestResolveAndErrors:
    @pytest.mark.asyncio
    async def test_resolve_sends_state_notes_and_configured_code(self):
        client = ServiceNowClient(
            ServiceNowSettings(
                base_url="https://example.service-now.com", close_code="Solution provided"
            )
        )
        with patch.object(client, "_patch", AsyncMock(return_value={"result": {}})) as patched:
            await client.resolve_incident(SYS_ID, "Verified by step-verify")
        assert patched.call_args.args[1] == {
            "state": "6",
            "close_notes": "Verified by step-verify",
            "close_code": "Solution provided",
        }

    @pytest.mark.asyncio
    async def test_error_carries_status_and_servicenow_message(self):
        from aiohttp import web
        from aiohttp.test_utils import TestServer

        from src.l1_agent.clients.servicenow_client import ServiceNowError

        async def deny(request):
            return web.json_response(
                {
                    "error": {
                        "message": "Operation Failed",
                        "detail": "Data Policy Exception: Resolution code is mandatory",
                    }
                },
                status=403,
            )

        app = web.Application()
        app.router.add_patch("/api/now/table/incident/{sys_id}", deny)
        server = TestServer(app)
        await server.start_server()
        client = ServiceNowClient(
            ServiceNowSettings(
                base_url=str(server.make_url("")).rstrip("/"), username="u", password="p-secret"
            )
        )
        try:
            with pytest.raises(ServiceNowError) as err:
                await client.resolve_incident(SYS_ID, "notes")
        finally:
            await client.close()
            await server.close()
        assert err.value.status == 403
        assert "Resolution code is mandatory" in str(err.value)
        assert "p-secret" not in str(err.value)
