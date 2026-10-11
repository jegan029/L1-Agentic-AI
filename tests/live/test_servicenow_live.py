"""Live tests against the configured ServiceNow instance.

Skipped unless SERVICENOW_BASE_URL, SERVICENOW_USERNAME and SERVICENOW_PASSWORD
are set (in the environment or in .env).

    pytest tests/live -m live_servicenow -v -s

THESE TESTS WRITE TO THE INSTANCE. They create their own incidents, whose short
description starts with "[L1-AGENT-TEST]", and only ever update those. They do
not touch any other record. Use a developer or sub-production instance.

Tool adapters are mocks; only ServiceNow is real.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from dotenv import load_dotenv

from src.l1_agent.clients.servicenow_client import ServiceNowClient, ServiceNowError
from src.l1_agent.config.settings import ServiceNowSettings
from src.l1_agent.demo import build_mock_adapters, load_sample_sops
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.models.evidence import ExecutionOutcome

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

_REQUIRED = ("SERVICENOW_BASE_URL", "SERVICENOW_USERNAME", "SERVICENOW_PASSWORD")
_missing = [name for name in _REQUIRED if not os.environ.get(name)]

pytestmark = [
    pytest.mark.live_servicenow,
    pytest.mark.skipif(bool(_missing), reason=f"ServiceNow not configured: {', '.join(_missing)}"),
    pytest.mark.timeout(120),
]

TAG = "[L1-AGENT-TEST]"
INTAKE_GROUP = os.environ.get("SERVICENOW_ASSIGNMENT_GROUP", "L1-Middleware-Support")


def settings() -> ServiceNowSettings:
    return ServiceNowSettings(
        base_url=os.environ["SERVICENOW_BASE_URL"],
        username=os.environ["SERVICENOW_USERNAME"],
        password=os.environ["SERVICENOW_PASSWORD"],
        assignment_group=INTAKE_GROUP,
        close_code=os.environ.get("SERVICENOW_CLOSE_CODE", ""),
    )


@pytest.fixture
async def client():
    c = ServiceNowClient(settings())
    yield c
    await c.close()


@pytest.fixture(autouse=True)
def no_email():
    with patch("src.l1_agent.escalation.escalator.send_escalation_email", return_value=False):
        yield


async def raw(client: ServiceNowClient, sys_id: str) -> Dict[str, Any]:
    result = await client._get(
        f"/api/now/table/incident/{sys_id}", {"sysparm_display_value": "all"}
    )
    return result["result"]


async def create_incident(client: ServiceNowClient, short: str, description: str) -> str:
    """Create a test incident in the intake group and return its sys_id."""
    run = uuid.uuid4().hex[:8]
    created = await client._request(
        "POST",
        "/api/now/table/incident",
        data={
            "short_description": f"{TAG} {short} ({run})",
            "description": description,
            "category": "Middleware",
            "subcategory": "MQ",
            "assignment_group": INTAKE_GROUP,
            "impact": "3",
            "urgency": "3",
        },
    )
    return created["result"]["sys_id"]


async def group_exists(client: ServiceNowClient, name: str) -> bool:
    result = await client._get(
        "/api/now/table/sys_user_group",
        {"sysparm_query": f"name={name}", "sysparm_fields": "sys_id,name", "sysparm_limit": "1"},
    )
    return bool(result.get("result"))


def make_processor(client: ServiceNowClient) -> IncidentProcessor:
    return IncidentProcessor(
        snow_client=client,
        sop_matcher=SOPMatcher(confidence_threshold=0.6),
        sop_parser=SOPParser(),
        executor=SOPExecutor(
            adapters=build_mock_adapters(), retry_max_attempts=1, retry_base_delay=0
        ),
        sop_cache=load_sample_sops(),
        confidence_threshold=0.6,
        l2_router=L2Router(),
    )


class TestConnection:
    @pytest.mark.asyncio
    async def test_authenticates_and_reads_incidents(self, client):
        result = await client._get(
            "/api/now/table/incident", {"sysparm_limit": "1", "sysparm_fields": "sys_id,number"}
        )
        print(f"\n  read ok, sample: {[r.get('number') for r in result['result']]}")
        assert isinstance(result["result"], list)

    @pytest.mark.asyncio
    async def test_wrong_password_is_reported_as_401(self):
        bad = ServiceNowClient(
            ServiceNowSettings(
                base_url=os.environ["SERVICENOW_BASE_URL"],
                username=os.environ["SERVICENOW_USERNAME"],
                password="definitely-not-the-password",
            )
        )
        try:
            with pytest.raises(ServiceNowError) as err:
                await bad._get("/api/now/table/incident", {"sysparm_limit": "1"})
        finally:
            await bad.close()
        assert err.value.status == 401
        assert "definitely-not-the-password" not in str(err.value)

    @pytest.mark.asyncio
    async def test_knowledge_table_is_readable(self, client):
        records = await client.get_sops(limit=3)
        print(f"\n  kb_knowledge readable, {len(records)} record(s) returned")
        assert isinstance(records, list)

    @pytest.mark.asyncio
    async def test_required_groups_exist(self, client):
        """Reassignment is by group name; a missing group silently clears the field."""
        routing = L2Router()._routing
        needed = {INTAKE_GROUP, routing["default_team"]["name"]}
        needed |= {team["name"] for team in routing.get("ci_mappings", {}).values()}
        missing = sorted([name for name in needed if not await group_exists(client, name)])
        print(f"\n  groups checked: {len(needed)}, missing: {missing}")
        assert INTAKE_GROUP not in missing, f"create the intake group '{INTAKE_GROUP}'"
        assert routing["default_team"]["name"] not in missing, "create the default L2 group"
        if missing:
            pytest.xfail(f"optional L2 groups missing: {missing}")


class TestRealPayloadShape:
    """Checks the hand-written fixtures against what the instance really returns."""

    @pytest.mark.asyncio
    async def test_polled_incident_fields_are_plain_values(self, client):
        sys_id = await create_incident(
            client, "payload shape check", "Created to check the Table API response shape."
        )
        polled = await client.get_new_incidents(limit=50)
        mine = next((i for i in polled if i.sys_id == sys_id), None)
        record = await raw(client, sys_id)
        shapes = {
            k: type(v).__name__
            for k, v in record.items()
            if k
            in ("sys_id", "number", "priority", "state", "assignment_group", "cmdb_ci", "caller_id")
        }
        print(f"\n  raw field shapes with display_value=all: {shapes}")

        assert mine is not None, "the new incident was not returned by the poll query"
        for name, value in mine.to_dict().items():
            assert isinstance(value, (str, int)), f"{name} is {type(value).__name__}"
        assert mine.sys_id == sys_id and len(mine.sys_id) == 32
        assert mine.number.startswith("INC")
        assert mine.assignment_group == INTAKE_GROUP
        assert mine.state == 1
        assert mine.short_description.startswith(TAG)
        await client.update_incident(sys_id, {"work_notes": "L1 agent test: shape check complete."})

    @pytest.mark.asyncio
    async def test_poll_only_returns_the_intake_group(self, client):
        polled = await client.get_new_incidents(limit=50)
        assert all(i.assignment_group == INTAKE_GROUP for i in polled)
        assert all(i.state == 1 for i in polled)


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_verified_resolution_is_written_to_the_instance(self, client):
        sys_id = await create_incident(
            client,
            "MQ queue depth high on ORDER.REQUEST",
            "Queue depth alert for ORDER.REQUEST on queue manager QMPROD01. "
            "The mock adapter reports this queue as drained.",
        )
        incident = await client.get_incident(sys_id)

        result = await make_processor(client).process_incident(incident)
        after = await raw(client, sys_id)
        print(
            f"\n  {incident.number}: outcome={result.summary.outcome_label} "
            f"state={after['state']['display_value']} close_code={after['close_code']['display_value']!r}"
        )

        assert result.summary.outcome == ExecutionOutcome.RESOLVED
        assert result.summary.verified_by == "step-verify"
        assert after["state"]["value"] == "6", (
            "the engine verified a resolution but the instance did not accept the Resolved "
            "state; check SERVICENOW_CLOSE_CODE and the service log"
        )
        assert "step-verify" in after["close_notes"]["display_value"]
        assert "SOP Execution Summary" in after["work_notes"]["display_value"]

    @pytest.mark.asyncio
    async def test_escalation_is_written_to_the_instance(self, client):
        sys_id = await create_incident(
            client,
            "MQ queue depth high on PAYMENT.REQUEST",
            "Queue depth alert for PAYMENT.REQUEST on queue manager QMPROD01. "
            "The mock adapter reports this queue as still backed up.",
        )
        incident = await client.get_incident(sys_id)

        result = await make_processor(client).process_incident(incident)
        after = await raw(client, sys_id)
        expected_group = L2Router().resolve_l2_team(incident.cmdb_ci)["name"]
        print(
            f"\n  {incident.number}: outcome={result.summary.outcome_label} "
            f"state={after['state']['display_value']} group={after['assignment_group']['display_value']!r}"
        )

        assert result.summary.outcome == ExecutionOutcome.ESCALATED
        assert after["state"]["value"] != "6", "an escalated incident must not be resolved"
        assert after["assignment_group"]["display_value"] == expected_group
        notes = after["work_notes"]["display_value"]
        assert "Escalation packet" in notes and "step-verify" in notes

    @pytest.mark.asyncio
    async def test_incident_with_no_matching_sop_is_routed_not_resolved(self, client):
        sys_id = await create_incident(
            client,
            "SSL certificate expiring in 3 days on payments-api.internal",
            "Certificate renewal failed. No SOP covers this.",
        )
        incident = await client.get_incident(sys_id)
        result = await make_processor(client).process_incident(incident)
        after = await raw(client, sys_id)
        assert result.summary.outcome == ExecutionOutcome.ESCALATED
        assert result.summary.step_results == []
        assert after["state"]["value"] != "6"


async def _list_test_incidents(client: ServiceNowClient) -> List[Dict[str, Any]]:
    result = await client._get(
        "/api/now/table/incident",
        {
            "sysparm_query": f"short_descriptionSTARTSWITH{TAG}",
            "sysparm_fields": "number,state,short_description",
            "sysparm_limit": "200",
        },
    )
    return result["result"]


@pytest.mark.asyncio
async def test_zz_report_test_incidents(client):
    """Not an assertion: lists what these tests have left on the instance."""
    records = await _list_test_incidents(client)
    print(f"\n  incidents on the instance tagged {TAG}: {len(records)}")
    for r in records[-10:]:
        print(f"    {r['number']} state={r['state']} {r['short_description'][:70]}")
