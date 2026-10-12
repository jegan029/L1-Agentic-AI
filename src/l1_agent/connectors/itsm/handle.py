"""The tenant's ITSM connection, wherever it currently comes from.

A deployment may be given its ServiceNow connection through environment
variables, or a tenant administrator may enter it in the connector set-up
screen, which stores it as a `connections` row like any other connector's.
The stored connection wins. It is re-read every few seconds, in every
process, so that what was entered takes effect without a restart and a
removed connection stops being used.

Everything that talks to the ITSM holds this object and calls it as it would
call the client. With no connection at all every call raises
`ItsmNotConnected`: the service still starts, so that the connection can be
entered, and takes no incidents until it has been.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any, Callable, List, Optional

from src.l1_agent.audit import log as audit
from src.l1_agent.config.settings import ConfigError, ServiceNowSettings, Settings
from src.l1_agent.connectors.itsm.servicenow import ServiceNowConnector
from src.l1_agent.connectors.sdk.base import CheckResult
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets, SecretError, SecretResolver
from src.l1_agent.utils.logging import get_logger

logger = get_logger("itsm")

REFRESH_SECONDS = 15.0
CONNECTOR_ID = "servicenow"
# Connection settings that may be entered, and the setting each one replaces.
_FIELDS = (
    "base_url",
    "auth",
    "username",
    "oauth_client_id",
    "assignment_group",
    "incident_table",
    "close_code",
    "change_assignment_group",
    "claim",
)


class ItsmNotConnected(RuntimeError):
    """No ITSM connection is configured, or the one that is cannot be used."""


def check_servicenow_config(snow: ServiceNowSettings, references: tuple = ()) -> None:
    """Refuse a real ServiceNow connection that is incomplete or unscoped.

    Without an assignment group the poller would pick up every new incident on
    the instance, write work notes to it and reassign it. `references` names
    the credentials that are supplied as references instead of values.
    """
    required = [
        ("SERVICENOW_BASE_URL", snow.base_url),
        ("SERVICENOW_ASSIGNMENT_GROUP", snow.assignment_group),
    ]
    if snow.auth == "oauth":
        required += [
            ("SERVICENOW_OAUTH_CLIENT_ID", snow.oauth_client_id),
            (
                "SERVICENOW_OAUTH_CLIENT_SECRET",
                snow.oauth_client_secret or "client_secret" in references,
            ),
        ]
    else:
        required += [
            ("SERVICENOW_USERNAME", snow.username),
            ("SERVICENOW_PASSWORD", snow.password or "password" in references),
        ]
    missing = [name for name, value in required if not value]
    if missing:
        raise ConfigError("ServiceNow connection needs: " + ", ".join(missing))
    if not snow.base_url.lower().startswith("https://"):
        raise ConfigError("SERVICENOW_BASE_URL must start with https://")


class ItsmHandle:
    def __init__(
        self,
        db: Any,
        tenant_id: uuid.UUID,
        settings: Settings,
        resolver: SecretResolver,
        fallback: Callable[[], Any],
        *,
        refresh_seconds: float = REFRESH_SECONDS,
    ) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._settings = settings
        self._resolver = resolver
        self._fallback = fallback  # the client built from the environment, or None
        self._refresh = refresh_seconds
        self._checked_at = 0.0
        self._digest = ""
        self._stored: Optional[ServiceNowConnector] = None
        self._error = ""

    def _build(self, connection: Any) -> ServiceNowConnector:
        base = self._settings.servicenow
        entered = {k: connection.config[k] for k in _FIELDS if k in connection.config}
        # A stored connection carries references only. Values from the environment are
        # for the environment's connection and are not mixed into this one.
        snow = replace(base, password="", oauth_client_secret="", **entered)
        secrets = ConnectionSecrets(self._resolver, connection.secret_refs)
        check_servicenow_config(snow, secrets.slots)
        return ServiceNowConnector(snow, secrets, demo_mode=self._settings.agent.demo_mode)

    async def _reload(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._checked_at and now - self._checked_at < self._refresh:
            return
        # Imported here: the hub builds this module's connector through the catalogue.
        from src.l1_agent.connectors.hub import stored_connections

        connection = (await stored_connections(self._db, self._tenant_id)).get(CONNECTOR_ID)
        self._checked_at = now
        if connection is not None and not connection.enabled:
            connection = None
        digest = (
            audit.digest([dict(connection.config), dict(connection.secret_refs)])
            if connection
            else ""
        )
        if digest == self._digest:
            return
        old, self._stored, self._digest, self._error = self._stored, None, digest, ""
        if old is not None:
            await old.close()
        if connection is None:
            return
        try:
            self._stored = self._build(connection)
            logger.info("Using the ServiceNow connection entered for this tenant")
        except (ConfigError, SecretError) as exc:
            self._error = str(exc)
            logger.error("The stored ServiceNow connection is not usable: %s", exc)

    async def current(self, *, fresh: bool = False) -> Optional[Any]:
        """The client to use now, or None when there is no usable connection."""
        await self._reload(force=fresh)
        if self._digest:
            return self._stored  # a stored connection that is broken does not fall back
        return self._fallback()

    async def reason(self) -> str:
        """Why there is no connection, for a person. Empty when there is one."""
        if await self.current() is not None:
            return ""
        return self._error or "No ServiceNow connection is configured"

    async def test(self) -> List[CheckResult]:
        client = await self.current(fresh=True)
        if client is None:
            return [CheckResult("connection", False, await self.reason())]
        tester = getattr(client, "test_connection", None)
        if tester is None:
            return [CheckResult("connection", True, "connected (no test for this client)")]
        return await tester()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            client = await self.current()
            if client is None:
                raise ItsmNotConnected(self._error or "No ServiceNow connection is configured")
            return await getattr(client, name)(*args, **kwargs)

        call.__name__ = name
        return call

    async def close(self) -> None:
        if self._stored is not None:
            await self._stored.close()
            self._stored = None
        fallback = self._fallback()
        if fallback is not None:
            await fallback.close()
