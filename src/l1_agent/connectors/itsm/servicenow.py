"""ServiceNow ITSM connector: incidents through the Table API.

Authentication is basic or OAuth 2.0. Lists are paged. Before the agent
works a ticket it claims it, and a ticket someone else already holds is
left alone.

Basic authentication, paging, the claim step and the hand-over to another
group have run against one ServiceNow developer instance (tests/live). OAuth
is covered only by contract tests written from the vendor documentation.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from src.l1_agent.config.settings import ConfigError, ServiceNowSettings
from src.l1_agent.connectors.itsm.base import ClaimResult
from src.l1_agent.connectors.sdk.http import (
    Auth,
    BasicAuth,
    ConnectorHttpError,
    HttpClient,
    OAuthClientCredentials,
)
from src.l1_agent.connectors.sdk.manifest import Family, Manifest, SecretSlot
from src.l1_agent.connectors.sdk.schema import closed
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets, LiteralSecrets
from src.l1_agent.models.incident import Incident, _raw_value
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("servicenow_client")

STATE_NEW = "1"
STATE_IN_PROGRESS = "2"
RESOLVED_STATE = "6"
CLAIMABLE_STATES = frozenset({STATE_NEW, STATE_IN_PROGRESS})
INCIDENT_FIELDS = (
    "sys_id,number,short_description,description,category,"
    "subcategory,cmdb_ci,assignment_group,priority,state,"
    "caller_id,sys_created_on"
)


# X-Total-Count of the list response this task read last, when the instance sent one.
_total_count: ContextVar[Optional[int]] = ContextVar("servicenow_total_count", default=None)


class ServiceNowError(ConnectorHttpError):
    """A Table API call failed. Carries the status and ServiceNow's own message."""

    def __init__(
        self, method: str, path: str, status: int, message: str = "", detail: str = ""
    ) -> None:
        super().__init__(method, path, status, message, detail)
        self.args = (f"ServiceNow {self.args[0]}",)


def _parse_error(body: Any) -> tuple:
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        return "", ""
    return str(error.get("message", ""))[:300], str(error.get("detail", ""))[:300]


class ServiceNowConnector:
    """Async client for the ServiceNow Table API."""

    manifest = Manifest(
        id="servicenow",
        version="1.1.0",
        family=Family.ITSM,
        display_name="ServiceNow ITSM",
        notes=(
            "The integration user needs read and write on the incident table for the intake "
            "group (the itil role on a stock instance) and read on sys_user."
        ),
        config=closed(
            {
                "base_url": {"type": "string"},
                "auth": {"type": "string", "enum": ["basic", "oauth"]},
                "username": {"type": "string"},
                "oauth_client_id": {"type": "string"},
                "assignment_group": {"type": "string"},
                "incident_table": {"type": "string"},
                "close_code": {"type": "string"},
                "claim": {"type": "boolean"},
            },
            required=["base_url"],
        ),
        secrets=(
            SecretSlot("password", "Password of the integration user", required=False),
            SecretSlot("client_secret", "OAuth client secret", required=False),
        ),
    )

    def __init__(
        self,
        settings: ServiceNowSettings,
        secrets: Optional[ConnectionSecrets] = None,
        *,
        demo_mode: bool = False,
    ) -> None:
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        # Without references the credentials are the ones read at start-up.
        self._secrets = secrets or LiteralSecrets(
            {"password": settings.password, "client_secret": settings.oauth_client_secret}
        )
        self._http = HttpClient(
            self._base_url,
            self._auth(demo_mode),
            demo_mode=demo_mode,
            verify_tls=settings.verify_ssl,
            timeout_seconds=settings.timeout_seconds,
            name="SERVICENOW_BASE_URL",
            error_parser=_parse_error,
        )
        self._agent_sys_id = ""

    def _auth(self, demo_mode: bool) -> Auth:
        s = self._settings
        if s.auth == "basic":
            return BasicAuth(s.username, self._secrets, "password")
        if s.auth != "oauth":
            raise ConfigError("SERVICENOW_AUTH must be 'basic' or 'oauth'")
        if not s.oauth_client_id:
            raise ConfigError("SERVICENOW_OAUTH_CLIENT_ID is required for OAuth")
        return OAuthClientCredentials(
            s.oauth_token_url or f"{self._base_url}/oauth_token.do",
            s.oauth_client_id,
            self._secrets,
            # With a user name the password grant is used; without one, client credentials.
            username=s.username if self._secrets.has("password") else "",
            verify_tls=s.verify_ssl,
        )

    async def close(self) -> None:
        await self._http.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            response = await self._http.request(method, path, params=params, json=data)
        except ConnectorHttpError as exc:
            raise ServiceNowError(method, path, exc.status, exc.message, exc.detail) from None
        try:
            _total_count.set(int(response.headers.get("X-Total-Count", "")))
        except ValueError:
            _total_count.set(None)
        return response.body if isinstance(response.body, dict) else {}

    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def _patch(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("PATCH", path, data=data)

    async def _get_all(self, path: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
        """Every row of a list, a page at a time, up to the configured ceiling.

        ServiceNow cuts each page before it applies access controls, so a page
        can come back short, or empty, while readable rows remain further on.
        The end of the list is therefore taken from X-Total-Count. Only when an
        instance does not send it does a short page mean the end.
        """
        size = max(1, self._settings.page_size)
        rows: List[Dict[str, Any]] = []
        for page in range(max(1, self._settings.max_pages)):
            _total_count.set(None)
            result = await self._get(
                path, {**params, "sysparm_limit": str(size), "sysparm_offset": str(page * size)}
            )
            batch = result.get("result", [])
            if not isinstance(batch, list):
                break
            rows.extend(batch)
            total = _total_count.get()
            if (page + 1) * size >= total if total is not None else len(batch) < size:
                return rows
        else:
            logger.warning("ServiceNow list %s stopped at %d rows; more remain", path, len(rows))
        return rows

    # ── Incident operations ───────────────────────────────────────────

    async def get_new_incidents(
        self,
        assignment_group: str = "",
        limit: int = 0,
    ) -> List[Incident]:
        """New incidents in the intake group, oldest first. `limit` 0 means all pages."""
        table = self._settings.incident_table
        query_parts = [f"state={STATE_NEW}"]
        group = assignment_group or self._settings.assignment_group
        if group:
            query_parts.append(f"assignment_group.name={group}")
        params = {
            "sysparm_query": "^".join(query_parts) + "^ORDERBYsys_created_on",
            "sysparm_display_value": "all",
            "sysparm_fields": INCIDENT_FIELDS,
        }
        records = await self._get_all(f"/api/now/table/{table}", params)
        if limit:
            records = records[:limit]
        metrics.increment("servicenow.incidents_polled", len(records))
        return [Incident.from_servicenow(r) for r in records]

    async def get_incident(self, sys_id: str) -> Incident:
        """Fetch a single incident by sys_id."""
        table = self._settings.incident_table
        result = await self._get(
            f"/api/now/table/{table}/{sys_id}", {"sysparm_display_value": "all"}
        )
        return Incident.from_servicenow(result.get("result", {}))

    async def update_incident(
        self,
        sys_id: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update incident fields (work_notes, state, etc.)."""
        table = self._settings.incident_table
        if self._settings.claim and "assignment_group" in fields and "assigned_to" not in fields:
            # Handing a claimed ticket to another group also gives up the assignment. A stock
            # instance refuses the move otherwise (business rule "Abort changes on group"),
            # and the receiving team should not get a ticket that still shows the agent.
            fields = {**fields, "assigned_to": ""}
        logger.info("Updating incident %s with fields: %s", sys_id, list(fields.keys()))
        result = await self._patch(f"/api/now/table/{table}/{sys_id}", fields)
        metrics.increment("servicenow.incident_updates")
        return result.get("result", {})

    async def add_work_note(self, sys_id: str, note: str) -> Dict[str, Any]:
        """Add a work note to an incident."""
        return await self.update_incident(sys_id, {"work_notes": note})

    async def resolve_incident(self, sys_id: str, close_notes: str) -> Dict[str, Any]:
        """Move an incident to Resolved.

        A stock instance has a data policy that rejects the Resolved state
        unless a resolution code and resolution notes are supplied.
        """
        fields: Dict[str, Any] = {"state": RESOLVED_STATE, "close_notes": close_notes[:4000]}
        if self._settings.close_code:
            fields["close_code"] = self._settings.close_code
        return await self.update_incident(sys_id, fields)

    # ── Claim ─────────────────────────────────────────────────────────

    async def _agent_user(self) -> str:
        """sys_id of the user the agent works as."""
        if self._agent_sys_id:
            return self._agent_sys_id
        name = self._settings.agent_user or self._settings.username
        if not name:
            raise ConfigError("SERVICENOW_AGENT_USER is required to claim tickets")
        result = await self._get(
            "/api/now/table/sys_user",
            {
                "sysparm_query": f"user_name={name}",
                "sysparm_fields": "sys_id",
                "sysparm_limit": "1",
            },
        )
        rows = result.get("result") or []
        sys_id = _raw_value(rows[0].get("sys_id", "")) if rows else ""
        if not sys_id:
            raise ConfigError(f"ServiceNow has no user named '{name}' to claim tickets as")
        self._agent_sys_id = sys_id
        return sys_id

    async def claim(self, incident: Incident) -> ClaimResult:
        """Assign the ticket to the agent, unless someone else already has it.

        The Table API has no conditional update, so there is a short window
        between the check and the update in which a person can take the
        ticket and be overwritten. The read after the update catches the
        opposite case, where their update lands after ours.
        """
        if not self._settings.claim:
            return ClaimResult(True, "claiming is switched off")
        me = await self._agent_user()
        path = f"/api/now/table/{self._settings.incident_table}/{incident.sys_id}"
        fields = {"sysparm_display_value": "false", "sysparm_fields": "state,assigned_to,active"}

        current = (await self._get(path, fields)).get("result", {})
        owner = _raw_value(current.get("assigned_to", ""))
        state = _raw_value(current.get("state", ""))
        if owner == me:
            return ClaimResult(True, "already assigned to the agent")
        if owner:
            return ClaimResult(False, "the ticket is assigned to someone else")
        if state not in CLAIMABLE_STATES:
            return ClaimResult(False, f"the ticket is no longer open (state {state or 'unknown'})")

        await self._patch(path, {"assigned_to": me, "state": STATE_IN_PROGRESS})
        after = (await self._get(path, fields)).get("result", {})
        if _raw_value(after.get("assigned_to", "")) != me:
            return ClaimResult(False, "someone else took the ticket at the same moment")
        metrics.increment("servicenow.incidents_claimed")
        return ClaimResult(True)

    # ── Knowledge base records ────────────────────────────────────────

    async def get_sops(self, query: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """Knowledge base records. Used by the knowledge connector; never executed directly."""
        table = self._settings.sop_table
        params: Dict[str, str] = {"sysparm_display_value": "true"}
        if query:
            params["sysparm_query"] = query
        records = await self._get_all(f"/api/now/table/{table}", params)
        return records[:limit] if limit else records

    async def get_sop_by_id(self, sys_id: str) -> Dict[str, Any]:
        """Fetch a single knowledge base record by sys_id."""
        table = self._settings.sop_table
        result = await self._get(f"/api/now/table/{table}/{sys_id}")
        return result.get("result", {})


# The name the rest of the code base has always used.
ServiceNowClient = ServiceNowConnector
