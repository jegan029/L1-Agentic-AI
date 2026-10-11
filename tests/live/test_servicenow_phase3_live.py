"""Live checks of what Phase 3 added to the ServiceNow connector: claim, paging, knowledge base.

Same rules as test_servicenow_live.py: opt-in (`-m live_servicenow`), writes only to incidents
it creates itself, tagged "[L1-AGENT-TEST]". Use a developer or sub-production instance.
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.connectors.knowledge.servicenow_kb import ServiceNowKnowledge
from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.models.evidence import ExecutionOutcome
from tests.live.test_servicenow_live import (
    INTAKE_GROUP,
    create_incident,
    make_processor,
    pytestmark,  # noqa: F401  (opt-in marker, skip when not configured, timeout)
    raw,
    settings,
)


@pytest.fixture
async def client():
    c = ServiceNowClient(settings())
    yield c
    await c.close()


async def park(client: ServiceNowClient, sys_id: str) -> None:
    """Take a test incident out of the intake queue so a running agent does not pick it up."""
    await client.update_incident(
        sys_id,
        {
            "work_notes": "L1 agent test: complete.",
            "assignment_group": L2Router().resolve_l2_team("")["name"],
        },
    )


async def another_user(client: ServiceNowClient, not_this: str) -> str:
    result = await client._get(
        "/api/now/table/sys_user",
        {
            "sysparm_query": f"active=true^sys_id!={not_this}",
            "sysparm_fields": "sys_id",
            "sysparm_limit": "1",
        },
    )
    return result["result"][0]["sys_id"]


class TestClaim:
    async def test_an_unassigned_ticket_is_claimed_and_claiming_again_is_harmless(self, client):
        sys_id = await create_incident(client, "claim check", "Created to check the claim step.")
        incident = await client.get_incident(sys_id)
        try:
            first = await client.claim(incident)
            after = await raw(client, sys_id)
            me = await client._agent_user()
            print(
                f"\n  {incident.number}: claimed={first.claimed} "
                f"assigned_to={after['assigned_to']['display_value']!r} "
                f"state={after['state']['display_value']!r}"
            )
            assert first.claimed, first.reason
            assert after["assigned_to"]["value"] == me
            assert after["state"]["value"] == "2", "a claimed ticket is In Progress"
            second = await client.claim(incident)
            assert second.claimed and "already" in second.reason
        finally:
            await park(client, sys_id)

    async def test_a_ticket_someone_else_holds_is_not_touched(self, client):
        sys_id = await create_incident(client, "held by a person", "Created to check the claim.")
        incident = await client.get_incident(sys_id)
        try:
            other = await another_user(client, await client._agent_user())
            await client.update_incident(sys_id, {"assigned_to": other})
            before = await raw(client, sys_id)

            result = await make_processor(client).process_incident(incident)
            after = await raw(client, sys_id)
            print(
                f"\n  {incident.number}: outcome={result.summary.outcome_label} "
                f"reason={result.summary.escalation_reason!r}"
            )
            assert result.summary.outcome == ExecutionOutcome.ABORTED
            assert result.summary.escalation_reason.startswith("Not claimed")
            assert after["assigned_to"]["value"] == other
            assert after["assignment_group"]["value"] == before["assignment_group"]["value"]
            assert after["state"]["value"] == before["state"]["value"]
            assert after["work_notes"]["display_value"] == before["work_notes"]["display_value"]
        finally:
            await park(client, sys_id)


class TestPaging:
    async def test_small_pages_return_the_same_incidents_as_one_large_page(self, client):
        made = [
            await create_incident(client, f"paging check {n}", "Created to check paging.")
            for n in range(3)
        ]
        small = ServiceNowClient(replace(settings(), page_size=1, max_pages=200))
        try:
            one_page = await client.get_new_incidents()
            many_pages = await small.get_new_incidents()
            print(
                f"\n  New incidents in {INTAKE_GROUP}: {len(one_page)} in one page, "
                f"{len(many_pages)} over pages of 1"
            )
            ids = [i.sys_id for i in many_pages]
            assert len(ids) == len(set(ids)), "no incident is returned twice"
            assert set(made) <= set(ids)
            assert ids == [i.sys_id for i in one_page], "same incidents, same order"

            capped = ServiceNowClient(replace(settings(), page_size=1, max_pages=2))
            try:
                assert len(await capped.get_new_incidents()) == 2, "the page ceiling holds"
            finally:
                await capped.close()
        finally:
            await small.close()
            for sys_id in made:
                await park(client, sys_id)


class TestKnowledgeBase:
    async def test_published_articles_are_listed_with_a_cursor_that_can_be_sent_back(self, client):
        small = ServiceNowClient(replace(settings(), page_size=10, max_pages=20))
        try:
            source = ServiceNowKnowledge(small)
            articles, cursor = await source.changed_since("")
            print(f"\n  published articles read: {len(articles)}, cursor: {cursor!r}")
            if not articles:
                pytest.skip("the instance has no published knowledge articles")
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", cursor)
            assert all(len(a.id) == 32 and a.revision for a in articles)
            assert [a.updated_at for a in articles] == sorted(a.updated_at for a in articles)

            later, same = await source.changed_since(cursor)
            assert all(a.updated_at > cursor for a in later)
            assert same >= cursor
            fetched = await source.fetch(articles[0].id)
            assert fetched is not None and fetched.id == articles[0].id
        finally:
            await small.close()
