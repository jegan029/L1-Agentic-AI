"""Unit tests for ServiceNow client (using mocked HTTP responses)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.config.settings import ServiceNowSettings


@pytest.fixture
def settings():
    return ServiceNowSettings(
        base_url="https://test.service-now.com",
        username="admin",
        password="test",
        assignment_group="L1-Support",
    )


@pytest.fixture
def client(settings):
    return ServiceNowClient(settings)


class TestServiceNowClient:
    @pytest.mark.asyncio
    async def test_get_new_incidents_parses_response(self, client):
        mock_response = {
            "result": [
                {
                    "sys_id": "abc123",
                    "number": "INC001",
                    "short_description": "Test incident",
                    "description": "Details",
                    "category": "Software",
                    "subcategory": "",
                    "cmdb_ci": "TestApp",
                    "assignment_group": "L1-Support",
                    "priority": "3",
                    "state": "1",
                    "caller_id": "user1",
                    "sys_created_on": "2026-01-01 00:00:00",
                }
            ]
        }

        with patch.object(client, "_get", new_callable=AsyncMock, return_value=mock_response):
            incidents = await client.get_new_incidents()

        assert len(incidents) == 1
        assert incidents[0].number == "INC001"
        assert incidents[0].sys_id == "abc123"
        assert incidents[0].short_description == "Test incident"

    @pytest.mark.asyncio
    async def test_get_new_incidents_empty(self, client):
        with patch.object(client, "_get", new_callable=AsyncMock, return_value={"result": []}):
            incidents = await client.get_new_incidents()
        assert incidents == []

    @pytest.mark.asyncio
    async def test_update_incident(self, client):
        mock_response = {"result": {"sys_id": "abc123"}}
        with patch.object(client, "_patch", new_callable=AsyncMock, return_value=mock_response):
            result = await client.update_incident("abc123", {"state": "2"})
        assert result["sys_id"] == "abc123"

    @pytest.mark.asyncio
    async def test_add_work_note(self, client):
        mock_response = {"result": {"sys_id": "abc123"}}
        with patch.object(client, "update_incident", new_callable=AsyncMock, return_value=mock_response["result"]):
            result = await client.add_work_note("abc123", "Test note")
        assert result["sys_id"] == "abc123"

    @pytest.mark.asyncio
    async def test_get_sops(self, client):
        mock_response = {
            "result": [
                {"sys_id": "sop1", "short_description": "SOP 1"},
                {"sys_id": "sop2", "short_description": "SOP 2"},
            ]
        }
        with patch.object(client, "_get", new_callable=AsyncMock, return_value=mock_response):
            sops = await client.get_sops()
        assert len(sops) == 2

    @pytest.mark.asyncio
    async def test_get_incident(self, client):
        mock_response = {
            "result": {
                "sys_id": "abc123",
                "number": "INC001",
                "short_description": "Test",
                "priority": "2",
                "state": "1",
            }
        }
        with patch.object(client, "_get", new_callable=AsyncMock, return_value=mock_response):
            incident = await client.get_incident("abc123")
        assert incident.number == "INC001"
