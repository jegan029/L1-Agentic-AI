"""Live checks of change records and approvals (ADR 0008). First run 2026-10-11, one developer instance.

Opt-in, like the other live tests, and it needs one more setting:

    LIVE_SERVICENOW_CHANGE_TEMPLATE="<name of an active standard change template>"
    LIVE_SERVICENOW_APPROVAL_GROUP="<a group with at least one active member>"   (optional)
    SERVICENOW_CHANGE_ASSIGNMENT_GROUP="<a group>"   (needed to close, unless the template sets one)
    pytest tests/live/test_servicenow_changes_live.py -m live_servicenow -v -s

THESE TESTS WRITE TO THE INSTANCE. Each creates one incident and one standard
change, both tagged "[L1-AGENT-TEST]", and with a group set, approval requests
on that change for the group's members, who will see them. Nothing is approved
by the test. Use a developer or sub-production instance.

What the first run settled: the create call is accepted as written, and a
standard change can be taken to Closed through the Table API once it has an
assignment group. Not covered here: a decision made by a person, and the least
roles the integration user needs.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace

import pytest

from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.connectors.itsm.base import ChangeRef
from tests.live.test_servicenow_live import (
    TAG,
    create_incident,
    settings,
)
from tests.live.test_servicenow_live import (
    pytestmark as _live,
)

TEMPLATE = os.environ.get("LIVE_SERVICENOW_CHANGE_TEMPLATE", "")
GROUP = os.environ.get("LIVE_SERVICENOW_APPROVAL_GROUP", "")
CHANGE_GROUP = os.environ.get("SERVICENOW_CHANGE_ASSIGNMENT_GROUP", "")
pytestmark = [
    *_live,
    pytest.mark.skipif(not TEMPLATE, reason="LIVE_SERVICENOW_CHANGE_TEMPLATE is not set"),
]


@pytest.fixture
async def client():
    c = ServiceNowClient(replace(settings(), change_assignment_group=CHANGE_GROUP))
    yield c
    await c.close()


async def open_change(client: ServiceNowClient, correlation: str) -> ChangeRef:
    incident = await create_incident(client, "change check", "Created to check change records.")
    return await client.open_change(
        TEMPLATE,
        correlation=correlation,
        short_description=f"{TAG} L1 Agent: change record check",
        description=f"Created by a test. Action hash: {correlation}",
        incident_sys_id=incident,
    )


class TestChangeRecord:
    async def test_a_standard_change_is_opened_once_and_carries_the_hash(self, client):
        correlation = uuid.uuid4().hex * 2
        change = await open_change(client, correlation)
        print(f"\n  opened {change.number} from template {TEMPLATE!r}")
        assert change.sys_id and change.number.startswith("CHG")

        again = await client.open_change(
            TEMPLATE,
            correlation=correlation,
            short_description="x",
            description="x",
            incident_sys_id="0" * 32,
        )
        assert again == change, "the same action does not open a second change"
        standing = await client.change_approval(change)
        assert standing.correlation == correlation
        assert standing.state == "requested", "nobody has been asked, so nobody has decided"

    async def test_a_template_that_does_not_exist_is_refused(self, client):
        from src.l1_agent.connectors.itsm.base import ChangeUnavailable

        with pytest.raises(ChangeUnavailable):
            await client.open_change(
                "No such template " + uuid.uuid4().hex[:8],
                correlation=uuid.uuid4().hex * 2,
                short_description="x",
                description="x",
                incident_sys_id="0" * 32,
            )

    async def test_a_change_is_noted_and_closed_with_the_result(self, client):
        change = await open_change(client, uuid.uuid4().hex * 2)
        await client.note_change(change, "L1 agent test: note on the change.")
        await client.close_change(change, True, "L1 agent test: closed as successful.")
        record = await client._get(
            f"/api/now/table/change_request/{change.sys_id}",
            {"sysparm_fields": "state,close_code", "sysparm_display_value": "false"},
        )
        print(f"\n  {change.number} after close: {record['result']}")
        assert record["result"]["state"] == "3"

    @pytest.mark.skipif(not GROUP, reason="LIVE_SERVICENOW_APPROVAL_GROUP is not set")
    async def test_group_members_are_asked_and_the_request_is_seen_as_waiting(self, client):
        change = await open_change(client, uuid.uuid4().hex * 2)
        asked = await client.request_change_approval(change, GROUP)
        again = await client.request_change_approval(change, GROUP)
        standing = await client.change_approval(change)
        print(f"\n  {change.number}: asked {asked} member(s) of {GROUP!r}; state {standing.state}")
        assert asked >= 1 and again == asked, "asking twice does not ask anyone twice"
        assert standing.state == "requested"
