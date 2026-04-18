"""ServiceNow REST API client for incidents and SOP/runbook retrieval."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp

from src.l1_agent.config.settings import ServiceNowSettings
from src.l1_agent.models.incident import Incident
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("servicenow_client")


class ServiceNowClient:
    """Async REST client for ServiceNow Table API."""

    def __init__(self, settings: ServiceNowSettings) -> None:
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        self._auth = aiohttp.BasicAuth(settings.username, settings.password)
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                auth=self._auth,
                headers=self._headers,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _patch(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        async with session.patch(url, json=data) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ── Incident operations ───────────────────────────────────────────

    async def get_new_incidents(
        self,
        assignment_group: str = "",
        limit: int = 20,
    ) -> List[Incident]:
        """Poll for new/open incidents in the specified assignment group."""
        table = self._settings.incident_table
        query_parts = ["state=1"]  # NEW
        group = assignment_group or self._settings.assignment_group
        if group:
            query_parts.append(f"assignment_group.name={group}")
        query = "^".join(query_parts)
        params = {
            "sysparm_query": query,
            "sysparm_limit": str(limit),
            "sysparm_display_value": "all",
            "sysparm_fields": (
                "sys_id,number,short_description,description,category,"
                "subcategory,cmdb_ci,assignment_group,priority,state,"
                "caller_id,sys_created_on"
            ),
        }
        result = await self._get(f"/api/now/table/{table}", params)
        records = result.get("result", [])
        metrics.increment("servicenow.incidents_polled", len(records))
        return [Incident.from_servicenow(r) for r in records]

    async def get_incident(self, sys_id: str) -> Incident:
        """Fetch a single incident by sys_id."""
        table = self._settings.incident_table
        result = await self._get(f"/api/now/table/{table}/{sys_id}")
        return Incident.from_servicenow(result.get("result", {}))

    async def update_incident(
        self,
        sys_id: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update incident fields (work_notes, state, etc.)."""
        table = self._settings.incident_table
        logger.info("Updating incident %s with fields: %s", sys_id, list(fields.keys()))
        result = await self._patch(f"/api/now/table/{table}/{sys_id}", fields)
        metrics.increment("servicenow.incident_updates")
        return result.get("result", {})

    async def add_work_note(self, sys_id: str, note: str) -> Dict[str, Any]:
        """Add a work note to an incident."""
        return await self.update_incident(sys_id, {"work_notes": note})

    # ── SOP / Runbook operations ──────────────────────────────────────

    async def get_sops(self, query: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve SOP/runbook records from the knowledge base."""
        table = self._settings.sop_table
        params: Dict[str, str] = {
            "sysparm_limit": str(limit),
            "sysparm_display_value": "true",
        }
        if query:
            params["sysparm_query"] = query
        result = await self._get(f"/api/now/table/{table}", params)
        return result.get("result", [])

    async def get_sop_by_id(self, sys_id: str) -> Dict[str, Any]:
        """Fetch a single SOP record by sys_id."""
        table = self._settings.sop_table
        result = await self._get(f"/api/now/table/{table}/{sys_id}")
        return result.get("result", {})
