"""ServiceNow change records and approvals against written conversations (ADR 0008).

The fixtures were written by hand from the Change Management API and Table API
documentation. They show what the connector sends and how it reads each kind
of answer. They do not show that a real instance answers that way: none of
this has run against one.
"""

from __future__ import annotations

import pytest

from src.l1_agent.config.settings import ServiceNowSettings
from src.l1_agent.connectors.itsm.base import ChangeRef, ChangeUnavailable
from src.l1_agent.connectors.itsm.servicenow import ServiceNowConnector, ServiceNowError
from tests.contract.replay import Replay

INC = f"{1:032x}"
CHANGE = ChangeRef("c" * 32, "CHG0030001")
HASH = "a" * 64
TEMPLATE = "L1 Agent - Autosys job restart"


def connector(url: str, change_group: str = "") -> ServiceNowConnector:
    settings = ServiceNowSettings(
        base_url=url,
        username="agent",
        password="pw-one",
        assignment_group="L1",
        page_size=50,
        change_assignment_group=change_group,
    )
    return ServiceNowConnector(settings, demo_mode=True)


async def open_change(fixture: str, change_group: str = "") -> tuple:
    async with Replay.from_file(f"servicenow/{fixture}.json") as server:
        snow = connector(server.url, change_group)
        try:
            change = await snow.open_change(
                TEMPLATE,
                correlation=HASH,
                short_description="L1 Agent: autosys.job.force_start for INC0000001",
                description="Action: autosys.job.force_start",
                incident_sys_id=INC,
            )
        finally:
            await snow.close()
        server.assert_complete()
        return change, server.requests


class TestOpenChange:
    async def test_a_standard_change_is_opened_from_the_named_template(self):
        change, requests = await open_change("change_open")
        assert change == CHANGE
        created = requests[2]
        assert created["method"] == "POST"
        assert created["json"]["correlation_id"] == HASH, "the change carries the action's hash"
        assert requests[3]["json"] == {"rfc": CHANGE.sys_id}, "the incident points at its change"

    async def test_a_change_that_already_exists_for_the_action_is_reused(self):
        change, requests = await open_change("change_open_existing")
        assert change == CHANGE and len(requests) == 1, "nothing was created a second time"

    async def test_a_missing_template_means_no_change(self):
        with pytest.raises(ChangeUnavailable, match="no active standard change template"):
            await open_change("change_open_no_template")

    async def test_a_refused_create_is_an_error_and_is_not_sent_twice(self):
        with pytest.raises(ServiceNowError) as err:
            await open_change("change_open_refused")  # the fixture has one POST
        assert err.value.status == 403 and "pw-one" not in str(err.value)

    async def test_the_change_stands_even_if_the_incident_cannot_be_linked(self):
        change, _ = await open_change("change_open_incident_link_refused")
        assert change == CHANGE

    async def test_without_a_configured_group_none_is_sent(self):
        _, requests = await open_change("change_open")
        assert "assignment_group" not in requests[2]["json"], "the template's own group stands"

    async def test_the_configured_group_is_put_on_the_change(self):
        """A stock instance will not move a change out of New without one."""
        change, requests = await open_change("change_open_with_group", "Change Management")
        assert change == CHANGE
        assert requests[3]["method"] == "POST"
        assert requests[3]["json"]["assignment_group"] == "d" * 32, "by sys_id, not by name"

    async def test_a_configured_group_that_does_not_exist_means_no_change(self):
        with pytest.raises(ChangeUnavailable, match="no active group 'Change Management'"):
            await open_change("change_open_no_group", "Change Management")  # no POST recorded

    @pytest.mark.parametrize("name", ["CAB^ORactive=true", "CAB\nNQactive=true"])
    async def test_a_group_name_cannot_carry_query_operators(self, name):
        async with Replay([]) as server:
            snow = connector(server.url, name)
            try:
                with pytest.raises(ChangeUnavailable, match="not a plain group name"):
                    await snow._change_group(name)
            finally:
                await snow.close()
            assert server.requests == [], "nothing was asked of the instance"

    async def test_a_template_name_cannot_carry_query_operators(self):
        """The name goes into a query. The SOP validator is what keeps it plain."""
        from src.l1_agent.models.sop import SOP
        from src.l1_agent.sop.validator import validate_sop
        from tests.actions_support import restart_content

        for name in ("x^ORactive=true", "x\nNQactive=true", "", "a" * 101):
            sop = SOP.from_dict({**restart_content(), "change_template": name})
            assert any("change_template" in p for p in validate_sop(sop)), repr(name)


class TestApprovalOnTheChange:
    async def test_each_group_member_is_asked_once(self):
        async with Replay.from_file("servicenow/change_request_approval.json") as server:
            snow = connector(server.url)
            try:
                asked = await snow.request_change_approval(CHANGE, "CAB")
            finally:
                await snow.close()
            server.assert_complete()  # one POST: Bob had been asked already, Alice is listed twice
        assert asked == 2

    async def test_an_empty_group_is_an_error_not_a_silent_wait(self):
        async with Replay.from_file(
            "servicenow/change_request_approval_empty_group.json"
        ) as server:
            snow = connector(server.url)
            try:
                with pytest.raises(ChangeUnavailable, match="no active members"):
                    await snow.request_change_approval(CHANGE, "CAB")
            finally:
                await snow.close()

    @pytest.mark.parametrize(
        "fixture, state, approver, comment",
        [
            ("change_approval_waiting", "requested", "", ""),
            ("change_approval_approved", "approved", "alice.cab", "ok to run"),
            ("change_approval_rejected_wins", "rejected", "bob.cab", "not during close"),
        ],
    )
    async def test_where_the_approval_stands(self, fixture, state, approver, comment):
        async with Replay.from_file(f"servicenow/{fixture}.json") as server:
            snow = connector(server.url)
            try:
                found = await snow.change_approval(CHANGE)
            finally:
                await snow.close()
            server.assert_complete()
        assert (found.state, found.approver, found.comment) == (state, approver, comment)
        assert found.correlation == HASH, (
            "read back from the change, to be compared with the action"
        )
        if approver:
            assert found.approver_email == f"{approver}@example.com"


class TestCloseChange:
    async def test_a_note_then_each_state_in_turn_then_closed_with_the_result(self):
        async with Replay.from_file("servicenow/change_close.json") as server:
            snow = connector(server.url)
            try:
                await snow.note_change(CHANGE, "L1 Agent ran autosys.job.force_start.")
                await snow.close_change(CHANGE, True, "Verified")
            finally:
                await snow.close()
            server.assert_complete()

    async def test_a_refused_state_change_is_an_error(self):
        async with Replay.from_file("servicenow/change_close_refused.json") as server:
            snow = connector(server.url)
            try:
                with pytest.raises(ServiceNowError) as err:
                    await snow.close_change(CHANGE, False, "not verified")
            finally:
                await snow.close()
            server.assert_complete()  # asked why once; the instance did not say
        assert err.value.status == 403
        assert "Invalid state transition" in str(err.value), "ServiceNow's own words are kept"

    async def test_a_refused_state_change_says_what_the_instance_still_needs(self):
        """Seen on a developer instance: a standard change with no assignment group."""
        async with Replay.from_file("servicenow/change_close_refused_with_reason.json") as server:
            snow = connector(server.url)
            try:
                with pytest.raises(ServiceNowError) as err:
                    await snow.close_change(CHANGE, True, "Verified")
            finally:
                await snow.close()
            server.assert_complete()  # the next state was not attempted
        assert err.value.status == 403
        assert "'New to Scheduled' needs: Assignment group is not empty" in str(err.value)
