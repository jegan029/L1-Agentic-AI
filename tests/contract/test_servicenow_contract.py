"""ServiceNow connector against recorded conversations (Phase 3 exit criterion 2).

The fixtures were written from the Table API and OAuth documentation. They
show what the connector sends and how it reads each kind of answer; they do
not show that a real instance answers that way.
"""

from __future__ import annotations

import base64

import pytest

from src.l1_agent.config.settings import ConfigError, ServiceNowSettings
from src.l1_agent.connectors.itsm.servicenow import ServiceNowConnector, ServiceNowError
from src.l1_agent.connectors.knowledge.servicenow_kb import ServiceNowKnowledge
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets, FileBackend, SecretResolver
from src.l1_agent.models.incident import Incident
from tests.contract.replay import Replay

INC = f"{1:032x}"
TICKET = Incident(sys_id=INC, number="INC0000001")


def connector(url: str, secrets=None, **overrides) -> ServiceNowConnector:
    values = dict(
        base_url=url,
        username="agent",
        password="pw-one",
        assignment_group="L1-Middleware-Support",
        page_size=2,
    )
    values.update(overrides)
    return ServiceNowConnector(ServiceNowSettings(**values), secrets, demo_mode=True)


class TestPaging:
    async def test_every_page_is_read_in_order(self):
        assert base64.b64encode(b"agent:pw-one").decode() == "YWdlbnQ6cHctb25l"
        async with Replay.from_file("servicenow/incidents_paged.json") as server:
            snow = connector(server.url)
            try:
                incidents = await snow.get_new_incidents()
            finally:
                await snow.close()
            server.assert_complete()
        assert [i.number for i in incidents] == [f"INC00{n:05d}" for n in range(1, 6)]
        assert incidents[0].assignment_group == "L1-Middleware-Support"
        assert incidents[0].cmdb_ci == "OrderService" and incidents[0].priority == 3

    async def test_a_short_or_empty_page_is_not_the_end_when_rows_are_hidden_by_access_control(
        self,
    ):
        # Seen on a real instance: the page is cut first and unreadable rows dropped after,
        # so the first page here is empty although two readable rows follow.
        async with Replay.from_file("servicenow/incidents_acl_filtered.json") as server:
            snow = connector(server.url)
            try:
                incidents = await snow.get_new_incidents()
            finally:
                await snow.close()
            server.assert_complete()
        assert [i.number for i in incidents] == ["INC0000003", "INC0000005"]

    async def test_paging_stops_at_the_configured_ceiling(self):
        async with Replay.from_file("servicenow/incidents_page_ceiling.json") as server:
            snow = connector(server.url, max_pages=2)
            try:
                incidents = await snow.get_new_incidents()
            finally:
                await snow.close()
            server.assert_complete()
        assert len(incidents) == 4, "two full pages and no third request"


class TestOAuth:
    async def test_client_credentials_token_is_fetched_once_and_reused(self):
        async with Replay.from_file("servicenow/oauth_client_credentials.json") as server:
            snow = connector(
                server.url,
                auth="oauth",
                password="",
                oauth_client_id="l1-agent",
                oauth_client_secret="cs-one",
            )
            try:
                first = await snow.get_incident(INC)
                second = await snow.get_incident(INC)
            finally:
                await snow.close()
            server.assert_complete()
        assert first.number == second.number == "INC0000001"

    async def test_password_grant_is_used_when_a_password_is_configured(self):
        async with Replay.from_file("servicenow/oauth_password_grant.json") as server:
            snow = connector(
                server.url, auth="oauth", oauth_client_id="l1-agent", oauth_client_secret="cs-one"
            )
            try:
                await snow.get_incident(INC)
            finally:
                await snow.close()
            server.assert_complete()

    async def test_a_rejected_token_is_replaced_and_the_call_repeated_once(self):
        async with Replay.from_file("servicenow/oauth_token_rejected.json") as server:
            snow = connector(
                server.url,
                auth="oauth",
                password="",
                oauth_client_id="l1-agent",
                oauth_client_secret="cs-one",
            )
            try:
                incident = await snow.get_incident(INC)
            finally:
                await snow.close()
            server.assert_complete()
        assert incident.number == "INC0000001"

    async def test_a_refused_token_request_is_an_error_without_the_secret(self):
        async with Replay.from_file("servicenow/oauth_denied.json") as server:
            snow = connector(
                server.url,
                auth="oauth",
                password="",
                oauth_client_id="l1-agent",
                oauth_client_secret="cs-one",
            )
            try:
                with pytest.raises(ServiceNowError) as err:
                    await snow.get_incident(INC)
            finally:
                await snow.close()
        assert err.value.status == 401
        assert "cs-one" not in str(err.value)

    def test_oauth_needs_a_client_id(self):
        with pytest.raises(ConfigError):
            connector("https://example.service-now.com", auth="oauth")


class TestClaim:
    async def run(self, fixture: str, **overrides):
        async with Replay.from_file(f"servicenow/{fixture}.json") as server:
            snow = connector(server.url, **overrides)
            try:
                result = await snow.claim(TICKET)
            finally:
                await snow.close()
            server.assert_complete()
        return result

    async def test_an_unassigned_new_ticket_is_claimed(self):
        result = await self.run("claim_ok")
        assert result.claimed

    async def test_a_ticket_someone_else_holds_is_left_alone(self):
        result = await self.run("claim_held")  # the fixture has no PATCH
        assert not result.claimed and "someone else" in result.reason

    async def test_claiming_again_is_harmless(self):
        result = await self.run("claim_already_ours")
        assert result.claimed

    async def test_a_ticket_that_is_no_longer_open_is_not_claimed(self):
        result = await self.run("claim_resolved")
        assert not result.claimed and "state 6" in result.reason

    async def test_losing_the_race_is_noticed_on_the_read_back(self):
        result = await self.run("claim_race")
        assert not result.claimed and "same moment" in result.reason

    async def test_a_refused_update_is_an_error_not_a_claim(self):
        with pytest.raises(ServiceNowError) as err:
            await self.run("claim_acl_denied")
        assert err.value.status == 403 and "ACL Exception" in str(err.value)

    async def test_handing_a_claimed_ticket_to_another_group_releases_the_assignment(self):
        # A stock instance refuses a group change while the ticket is still assigned
        # (business rule "Abort changes on group"). Seen on a developer instance.
        from unittest.mock import AsyncMock, patch

        for claim, expected in (
            (True, {"assignment_group": "L2-General", "assigned_to": ""}),
            (False, {"assignment_group": "L2-General"}),
        ):
            snow = connector("https://example.service-now.com", claim=claim)
            with patch.object(snow, "_patch", AsyncMock(return_value={"result": {}})) as sent:
                await snow.update_incident(INC, {"assignment_group": "L2-General"})
                await snow.update_incident(INC, {"work_notes": "a note"})
            await snow.close()
            assert sent.call_args_list[0].args[1] == expected
            assert sent.call_args_list[1].args[1] == {"work_notes": "a note"}

    async def test_claiming_can_be_switched_off(self):
        snow = connector("https://example.service-now.com", claim=False)
        try:
            assert (await snow.claim(TICKET)).claimed
        finally:
            await snow.close()


class TestErrors:
    async def test_a_data_policy_refusal_carries_servicenows_reason(self):
        async with Replay.from_file("servicenow/resolve_data_policy.json") as server:
            snow = connector(server.url)
            try:
                with pytest.raises(ServiceNowError) as err:
                    await snow.resolve_incident(INC, "Verified by step-verify")
            finally:
                await snow.close()
            server.assert_complete()
        assert err.value.status == 403
        assert "Resolution code is mandatory" in str(err.value)
        assert "pw-one" not in str(err.value)

    async def test_a_throttled_read_is_tried_again(self):
        async with Replay.from_file("servicenow/throttled_then_ok.json") as server:
            snow = connector(server.url)
            try:
                incident = await snow.get_incident(INC)
            finally:
                await snow.close()
            server.assert_complete()
        assert incident.number == "INC0000001"

    async def test_a_throttled_write_is_not_sent_twice(self):
        async with Replay.from_file("servicenow/write_not_retried.json") as server:
            snow = connector(server.url)
            try:
                with pytest.raises(ServiceNowError) as err:
                    await snow.add_work_note(INC, "note")
            finally:
                await snow.close()
            server.assert_complete()  # exactly one PATCH was recorded
        assert err.value.status == 429

    async def test_a_sleeping_instance_is_an_error_after_the_retries(self, monkeypatch):
        async def no_wait(_seconds):
            return None

        monkeypatch.setattr("src.l1_agent.connectors.sdk.http.asyncio.sleep", no_wait)
        async with Replay.from_file("servicenow/hibernating.json") as server:
            snow = connector(server.url)
            try:
                with pytest.raises(ServiceNowError) as err:
                    await snow.get_incident(INC)
            finally:
                await snow.close()
            server.assert_complete()
        assert err.value.status == 503 and "hibernating" not in str(err.value)

    def test_plain_http_is_refused_outside_demo_mode(self):
        with pytest.raises(ConfigError):
            ServiceNowConnector(ServiceNowSettings(base_url="http://example.service-now.com"))

    def test_tls_verification_cannot_be_switched_off_outside_demo_mode(self):
        with pytest.raises(ConfigError):
            ServiceNowConnector(
                ServiceNowSettings(base_url="https://example.service-now.com", verify_ssl=False)
            )


class TestRotation:
    async def test_a_rotated_password_is_used_on_the_next_call_without_a_restart(self, tmp_path):
        """Phase 3 exit criterion 5, for the ITSM connection."""
        assert base64.b64encode(b"agent:pw-two").decode() == "YWdlbnQ6cHctdHdv"
        secret = tmp_path / "servicenow-password"
        secret.write_text("pw-one", encoding="utf-8")
        resolver = SecretResolver({"file": FileBackend()}, ttl_seconds=3600)
        secrets = ConnectionSecrets(resolver, {"password": f"file:{secret.as_posix()}"})
        async with Replay.from_file("servicenow/password_rotated.json") as server:
            snow = connector(server.url, secrets, password="")
            try:
                await snow.get_incident(INC)
                # Rotation: the instance now refuses the old password and the
                # secrets backend holds the new one. The connector is not rebuilt.
                secret.write_text("pw-two", encoding="utf-8")
                incident = await snow.get_incident(INC)
            finally:
                await snow.close()
            server.assert_complete()
        assert incident.number == "INC0000001"


class TestKnowledgeBase:
    async def test_articles_are_paged_and_the_cursor_is_the_newest_change(self):
        async with Replay.from_file("servicenow/kb_paged.json") as server:
            snow = connector(server.url)
            try:
                articles, cursor = await ServiceNowKnowledge(snow).changed_since("")
            finally:
                await snow.close()
            server.assert_complete()
        assert [a.metadata["number"] for a in articles] == ["KB0000001", "KB0000002", "KB0000003"]
        assert cursor == "2026-03-03 08:00:00"
        assert articles[0].revision == "1@2026-03-01 08:00:00"
        assert articles[1].body.startswith('{"sop_id"')

    async def test_only_changes_after_the_cursor_are_asked_for(self):
        async with Replay.from_file("servicenow/kb_since_cursor.json") as server:
            snow = connector(server.url)
            try:
                articles, cursor = await ServiceNowKnowledge(snow).changed_since(
                    "2026-03-03 08:00:00"
                )
            finally:
                await snow.close()
            server.assert_complete()
        assert articles == [] and cursor == "2026-03-03 08:00:00"

    async def test_a_cursor_that_is_not_a_timestamp_is_never_sent(self):
        snow = connector("https://example.service-now.com")
        try:
            with pytest.raises(ValueError):
                await ServiceNowKnowledge(snow).changed_since("x^ORassigned_to=admin")
        finally:
            await snow.close()

    def test_a_filter_with_unexpected_characters_is_refused(self):
        with pytest.raises(ValueError):
            ServiceNowKnowledge(object(), "workflow_state=published\n^NQactive=true;")

    async def test_a_refused_read_is_an_error(self):
        async with Replay.from_file("servicenow/kb_forbidden.json") as server:
            snow = connector(server.url)
            try:
                with pytest.raises(ServiceNowError) as err:
                    await ServiceNowKnowledge(snow).changed_since("")
            finally:
                await snow.close()
        assert err.value.status == 403
