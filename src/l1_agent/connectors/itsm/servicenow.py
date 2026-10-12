"""ServiceNow ITSM connector: incidents through the Table API.

Authentication is basic or OAuth 2.0. Lists are paged. Before the agent
works a ticket it claims it, and a ticket someone else already holds is
left alone.

Basic authentication, paging, the claim step and the hand-over to another
group have run against one ServiceNow developer instance (tests/live). OAuth,
and everything to do with change records and approvals, is covered only by
contract tests written from the vendor documentation.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from src.l1_agent.config.settings import ConfigError, ServiceNowSettings
from src.l1_agent.connectors.itsm.base import (
    ChangeApproval,
    ChangeRef,
    ChangeUnavailable,
    ClaimResult,
)
from src.l1_agent.connectors.sdk.base import CheckResult
from src.l1_agent.connectors.sdk.http import (
    Auth,
    BasicAuth,
    ConnectorHttpError,
    HttpClient,
    OAuthClientCredentials,
)
from src.l1_agent.connectors.sdk.manifest import Family, Manifest, SecretSlot
from src.l1_agent.connectors.sdk.schema import closed
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets, LiteralSecrets, SecretError
from src.l1_agent.models.incident import Incident, _raw_value
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("servicenow_client")

STATE_NEW = "1"
STATE_IN_PROGRESS = "2"
RESOLVED_STATE = "6"
CLAIMABLE_STATES = frozenset({STATE_NEW, STATE_IN_PROGRESS})
# A standard change on a stock instance: Scheduled, Implement, Review, then Closed.
CHANGE_STATES_TO_CLOSE = ("-2", "-1", "0")
CHANGE_CLOSED = "3"
MAX_APPROVERS = 25
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
            "group (the itil role on a stock instance) and read on sys_user. For actions it "
            "also needs to create standard changes from the templates SOPs name, and, when "
            "approvals are collected in ServiceNow, to read sys_user_grmember and to create "
            "and read sysapproval_approver records."
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
                "change_assignment_group": {"type": "string"},
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

    async def test_connection(self) -> List[CheckResult]:
        """Sign in and make the two reads intake depends on. Nothing is written."""
        table = self._settings.incident_table
        reads = [
            ("read the incident table", f"/api/now/table/{table}", "sys_id"),
            ("read users (needed to claim a ticket)", "/api/now/table/sys_user", "sys_id"),
        ]
        checks: List[CheckResult] = []
        for name, path, fields in reads:
            try:
                await self._get(path, {"sysparm_limit": "1", "sysparm_fields": fields})
                checks.append(CheckResult(name, True))
            except ServiceNowError as exc:
                checks.append(CheckResult(name, False, str(exc)[:300]))
            except SecretError as exc:
                return checks + [CheckResult("credentials", False, str(exc))]
            except Exception as exc:  # unreachable host, TLS failure
                return checks + [CheckResult("reach the instance", False, type(exc).__name__)]
        return checks

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

    # ── Change records and approvals (ADR 0008) ───────────────────────
    #
    # Written from the Change Management API and Table API documentation.
    # Open, note, ask and close have run against one developer instance; reading a
    # person's decision has not.

    async def open_change(
        self,
        template: str,
        *,
        correlation: str,
        short_description: str,
        description: str,
        incident_sys_id: str,
    ) -> ChangeRef:
        """Open a standard change from a named template, once per `correlation`.

        The correlation (the action's hash) is stored on the change. A second
        call with the same one returns the change that already exists.
        """
        found = await self._get(
            "/api/now/table/change_request",
            {
                "sysparm_query": f"correlation_id={correlation}",
                "sysparm_fields": "sys_id,number",
                "sysparm_limit": "1",
            },
        )
        rows = found.get("result") or []
        if rows:
            return ChangeRef(_raw_value(rows[0].get("sys_id", "")), _raw_value(rows[0]["number"]))

        templates = await self._get(
            "/api/now/table/std_change_record_producer",
            {
                "sysparm_query": f"name={template}^active=true",
                "sysparm_fields": "sys_id",
                "sysparm_limit": "1",
            },
        )
        rows = templates.get("result") or []
        template_id = _raw_value(rows[0].get("sys_id", "")) if rows else ""
        if not template_id:
            raise ChangeUnavailable(f"there is no active standard change template '{template}'")
        fields = {
            "short_description": short_description,
            "description": description,
            "correlation_id": correlation,
        }
        group = self._settings.change_assignment_group
        if group:
            fields["assignment_group"] = await self._change_group(group)
        created = await self._request(
            "POST", f"/api/sn_chg_rest/change/standard/{template_id}", data=fields
        )
        result = created.get("result", {})
        change = ChangeRef(
            _raw_value(result.get("sys_id", "")), _raw_value(result.get("number", ""))
        )
        if not change.sys_id or not change.number:
            raise ChangeUnavailable("ServiceNow did not return the change it was asked to open")
        metrics.increment("servicenow.changes_opened")
        try:
            # The incident shows which change it was worked under.
            await self._patch(
                f"/api/now/table/{self._settings.incident_table}/{incident_sys_id}",
                {"rfc": change.sys_id},
            )
        except ServiceNowError as exc:
            logger.warning("Could not link %s to its incident: %s", change.number, exc.status)
        return change

    async def _change_group(self, name: str) -> str:
        """The sys_id of the group changes are assigned to. No group, no change."""
        if "^" in name or "\n" in name or "\r" in name:
            raise ChangeUnavailable("the change assignment group is not a plain group name")
        found = await self._get(
            "/api/now/table/sys_user_group",
            {
                "sysparm_query": f"name={name}^active=true",
                "sysparm_fields": "sys_id",
                "sysparm_limit": "1",
            },
        )
        rows = found.get("result") or []
        group_id = _raw_value(rows[0].get("sys_id", "")) if rows else ""
        if not group_id:
            raise ChangeUnavailable(f"there is no active group '{name}' to assign the change to")
        return str(group_id)

    async def _why_not(self, change: ChangeRef, state: str) -> str:
        """What the instance says a move to `state` still needs. Empty when it does not say."""
        try:
            got = await self._get(f"/api/sn_chg_rest/change/{change.sys_id}/nextstates")
        except ServiceNowError:
            return ""
        result = got.get("result")
        groups = result.get("state_transitions", []) if isinstance(result, dict) else []
        for transition in (t for group in groups if isinstance(group, list) for t in group):
            if not isinstance(transition, dict) or str(transition.get("to_state")) != state:
                continue
            unmet = [
                str((c.get("condition") or {}).get("name", ""))
                for c in transition.get("conditions") or []
                if isinstance(c, dict) and not c.get("passed")
            ]
            unmet = [name for name in unmet if name]
            if unmet:
                name = str(transition.get("display_value", "")) or f"the move to state {state}"
                return f"'{name}' needs: {'; '.join(unmet)}"[:200]
        return ""

    async def request_change_approval(self, change: ChangeRef, group: str) -> int:
        """Ask each active member of a group to approve the change. Returns how many."""
        members = await self._get_all(
            "/api/now/table/sys_user_grmember",
            {"sysparm_query": f"group.name={group}^user.active=true", "sysparm_fields": "user"},
        )
        wanted: List[str] = []
        for member in members:
            user = _raw_value(member.get("user", ""))
            if user and user not in wanted:
                wanted.append(user)
        wanted = wanted[:MAX_APPROVERS]
        if not wanted:
            raise ChangeUnavailable(f"approval group '{group}' has no active members")
        asked = await self._get_all(
            "/api/now/table/sysapproval_approver",
            {"sysparm_query": f"sysapproval={change.sys_id}", "sysparm_fields": "approver"},
        )
        already = {_raw_value(row.get("approver", "")) for row in asked}
        for user in wanted:
            if user in already:
                continue
            await self._request(
                "POST",
                "/api/now/table/sysapproval_approver",
                data={
                    "sysapproval": change.sys_id,
                    "document_id": change.sys_id,
                    "source_table": "change_request",
                    "approver": user,
                    "state": "requested",
                },
            )
        return len(wanted)

    async def change_approval(self, change: ChangeRef) -> ChangeApproval:
        """Where the approval stands. One rejection rejects; otherwise one approval approves."""
        record = await self._get(
            f"/api/now/table/change_request/{change.sys_id}",
            {"sysparm_fields": "correlation_id", "sysparm_display_value": "false"},
        )
        correlation = _raw_value(record.get("result", {}).get("correlation_id", ""))
        rows = await self._get_all(
            "/api/now/table/sysapproval_approver",
            {
                "sysparm_query": f"sysapproval={change.sys_id}^ORDERBYsys_updated_on",
                "sysparm_fields": "state,approver.user_name,approver.email,comments",
                "sysparm_display_value": "false",
            },
        )
        for wanted in ("rejected", "approved"):
            for row in rows:
                if _raw_value(row.get("state", "")) == wanted:
                    return ChangeApproval(
                        state=wanted,
                        approver=str(_raw_value(row.get("approver.user_name", ""))),
                        approver_email=str(_raw_value(row.get("approver.email", ""))),
                        comment=str(_raw_value(row.get("comments", "")))[:500],
                        correlation=correlation,
                    )
        return ChangeApproval(state="requested", correlation=correlation)

    async def note_change(self, change: ChangeRef, note: str) -> None:
        await self._patch(f"/api/now/table/change_request/{change.sys_id}", {"work_notes": note})

    async def close_change(self, change: ChangeRef, successful: bool, notes: str) -> None:
        """Take the change through its states to Closed, with the result."""
        path = f"/api/now/table/change_request/{change.sys_id}"
        for state in CHANGE_STATES_TO_CLOSE:
            try:
                await self._patch(path, {"state": state})
            except ServiceNowError as exc:
                # The refusal itself names a business rule; the instance can say what is missing.
                why = await self._why_not(change, state)
                if not why:
                    raise
                raise ServiceNowError("PATCH", path, exc.status, exc.message, why) from None
        await self._patch(
            path,
            {
                "state": CHANGE_CLOSED,
                "close_code": "successful" if successful else "unsuccessful",
                "close_notes": notes[:4000],
            },
        )

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
