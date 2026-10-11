"""A tenant's connections, and the connectors built from them.

A connection is a row: which connector, its settings, and references to its
credentials. A tenant without a row for a connector falls back to the
deployment's environment settings, so a single-tenant install configured
through environment variables keeps working.

Connections are re-read every few seconds, so a changed setting or
credential reference takes effect without a restart.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

import sqlalchemy as sa

from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.config.settings import ConfigError, Settings
from src.l1_agent.connectors.catalog import default_registry
from src.l1_agent.connectors.knowledge.base import KnowledgeSource
from src.l1_agent.connectors.knowledge.git import GitKnowledge
from src.l1_agent.connectors.knowledge.servicenow_kb import ServiceNowKnowledge
from src.l1_agent.connectors.sdk.base import CheckResult, Connector
from src.l1_agent.connectors.sdk.manifest import Family, Manifest, validate_parameters
from src.l1_agent.connectors.sdk.registry import ConnectorSet, Registry
from src.l1_agent.connectors.sdk.secrets import (
    ConnectionSecrets,
    SecretError,
    SecretRef,
    SecretResolver,
)
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.db.models import connections
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.secrets import _SECRET_MAPPINGS

logger = get_logger("connections")

REFRESH_SECONDS = 15.0
_VAULT_KEYS = {m["env_key"]: m["vault_key"] for m in _SECRET_MAPPINGS}


@dataclass(frozen=True)
class Connection:
    connector_id: str
    config: Mapping[str, Any]
    secret_refs: Mapping[str, str]
    enabled: bool = True
    origin: str = "connection"  # connection | environment


# ── Stored connections ────────────────────────────────────────────────


def check_connection(
    registry: Registry, connector_id: str, config: Any, secret_refs: Any
) -> Manifest:
    """Raise ConfigError unless the settings and references fit the connector's manifest."""
    cls = registry.connector_class(connector_id)
    if cls is None:
        raise ConfigError(f"Unknown connector '{connector_id}'")
    manifest: Manifest = cls.manifest
    if not isinstance(config, dict) or not isinstance(secret_refs, dict):
        raise ConfigError("config and secrets must be objects")
    if manifest.config:
        problems = validate_parameters(
            manifest.config, config, subject=f"connection '{connector_id}'"
        )
        if problems:
            raise ConfigError("; ".join(problems))
    slots = {s.name for s in manifest.secrets}
    for slot, reference in secret_refs.items():
        if slot not in slots:
            raise ConfigError(f"'{connector_id}' has no credential named '{slot}'")
        try:
            SecretRef.parse(str(reference))
        except SecretError as exc:
            raise ConfigError(f"credential '{slot}': {exc}") from None
    return manifest


async def set_connection(
    db: Database,
    tenant_id: uuid.UUID,
    actor: Actor,
    connector_id: str,
    config: Dict[str, Any],
    secret_refs: Dict[str, str],
    *,
    enabled: bool = True,
    registry: Optional[Registry] = None,
) -> None:
    check_connection(registry or default_registry(), connector_id, config, secret_refs)
    now = utcnow()
    values = {
        "config": config,
        "secret_refs": secret_refs,
        "enabled": enabled,
        "updated_by": f"{actor.type}:{actor.id}",
        "updated_at": now,
    }
    async with db.tenant(tenant_id) as conn:
        updated = await conn.execute(
            sa.update(connections)
            .where(connections.c.tenant_id == tenant_id, connections.c.connector_id == connector_id)
            .values(**values)
        )
        if not updated.rowcount:
            await conn.execute(
                sa.insert(connections).values(
                    id=uuid.uuid4(), tenant_id=tenant_id, connector_id=connector_id, **values
                )
            )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "connection.updated",
            {
                "connector": connector_id,
                "enabled": enabled,
                "config_sha256": audit.digest(config),
                # References say where a credential is kept. They are not credentials.
                "credentials": {slot: str(ref) for slot, ref in sorted(secret_refs.items())},
            },
        )


async def remove_connection(
    db: Database, tenant_id: uuid.UUID, actor: Actor, connector_id: str
) -> bool:
    async with db.tenant(tenant_id) as conn:
        removed = await conn.execute(
            sa.delete(connections).where(
                connections.c.tenant_id == tenant_id, connections.c.connector_id == connector_id
            )
        )
        if removed.rowcount:
            await audit.append(
                conn, tenant_id, actor, "connection.removed", {"connector": connector_id}
            )
    return bool(removed.rowcount)


async def stored_connections(db: Database, tenant_id: uuid.UUID) -> Dict[str, Connection]:
    async with db.tenant(tenant_id) as conn:
        rows = await conn.execute(
            sa.select(connections).where(connections.c.tenant_id == tenant_id)
        )
        return {
            r.connector_id: Connection(r.connector_id, r.config, r.secret_refs, r.enabled)
            for r in rows
        }


# ── Connections from the environment ──────────────────────────────────


def _reference(settings: Settings, name: str) -> str:
    """Where the credential called `name` is kept, for a deployment configured by environment."""
    explicit = settings.secret_refs.get(name)
    if explicit:
        return explicit
    backend = settings.secrets.backend
    if backend in ("vault", "aws") and name in _VAULT_KEYS:
        return f"{backend}:{_VAULT_KEYS[name]}"
    return f"env:{name}" if os.environ.get(name) else ""


def environment_connections(settings: Settings) -> Dict[str, Connection]:
    def made(connector_id: str, config: Dict[str, Any], **refs: str) -> Connection:
        return Connection(
            connector_id, config, {k: v for k, v in refs.items() if v}, origin="environment"
        )

    found: Dict[str, Connection] = {}
    s = settings
    if s.splunk.base_url:
        found["splunk"] = made(
            "splunk",
            {
                "base_url": s.splunk.base_url,
                "verify_ssl": s.splunk.verify_ssl,
                "search_timeout_seconds": s.splunk.search_timeout_seconds,
            },
            token=_reference(s, "SPLUNK_TOKEN"),
        )
    if s.dynatrace.base_url:
        found["dynatrace"] = made(
            "dynatrace",
            {
                "base_url": s.dynatrace.base_url,
                "verify_ssl": s.dynatrace.verify_ssl,
                "timeout_seconds": s.dynatrace.timeout_seconds,
            },
            api_token=_reference(s, "DYNATRACE_API_TOKEN"),
        )
    if s.ir360.base_url or s.ir360.use_cli:
        found["ibm_mq"] = made(
            "ibm_mq",
            {
                "base_url": s.ir360.base_url,
                "verify_ssl": s.ir360.verify_ssl,
                "use_cli": s.ir360.use_cli,
                "cli_path": s.ir360.cli_path,
            },
            api_key=_reference(s, "IR360_API_KEY"),
        )
    found["autosys"] = made(
        "autosys",
        {
            "cli_path": s.autosys.cli_path,
            "allowed_commands": list(s.autosys.allowed_commands),
            "sendevent_path": s.autosys.sendevent_path,
            "force_start_jobs": list(s.autosys.force_start_jobs),
        },
    )
    if s.windows_share.allowed_unc_prefixes:
        found["fileshare"] = made(
            "fileshare",
            {
                "allowed_unc_prefixes": list(s.windows_share.allowed_unc_prefixes),
                "smb_username": s.windows_share.smb_username,
                "smb_domain": s.windows_share.smb_domain,
            },
            smb_password=_reference(s, "SMB_PASSWORD"),
        )
    if s.webui.allowed_domains:
        found["browser"] = made(
            "browser",
            {
                "allowed_domains": list(s.webui.allowed_domains),
                "page_timeout_seconds": s.webui.page_timeout_seconds,
                "chrome_binary_path": s.webui.chrome_binary_path,
            },
        )
    if s.mainframe.host:
        found["mainframe"] = made(
            "mainframe",
            {
                "host": s.mainframe.host,
                "port": s.mainframe.port,
                "use_tls": s.mainframe.use_tls,
                "allowed_transactions": list(s.mainframe.allowed_transactions),
                "default_navigation": list(s.mainframe.default_navigation),
                "timeout_seconds": s.mainframe.timeout_seconds,
            },
        )
    k = s.knowledge
    if k.servicenow_kb:
        found["servicenow_kb"] = made("servicenow_kb", {"query": k.servicenow_kb_query})
    if k.git_url:
        found["git_knowledge"] = made(
            "git_knowledge",
            {"url": k.git_url, "ref": k.git_ref, "path": k.git_path, "cache_dir": k.git_cache_dir},
            token=_reference(s, "KNOWLEDGE_GIT_TOKEN"),
        )
    return found


def servicenow_secrets(settings: Settings, resolver: SecretResolver) -> Optional[ConnectionSecrets]:
    """References for the ITSM connection, or None to use the values read at start-up."""
    refs = {
        "password": settings.secret_refs.get("SERVICENOW_PASSWORD", ""),
        "client_secret": settings.secret_refs.get("SERVICENOW_OAUTH_CLIENT_SECRET", ""),
    }
    if settings.secrets.backend in ("vault", "aws"):
        # The credential the chosen authentication uses, read from the backend when needed.
        if settings.servicenow.auth == "oauth":
            refs["client_secret"] = refs["client_secret"] or _reference(
                settings, "SERVICENOW_OAUTH_CLIENT_SECRET"
            )
        else:
            refs["password"] = refs["password"] or _reference(settings, "SERVICENOW_PASSWORD")
    refs = {slot: ref for slot, ref in refs.items() if ref}
    return ConnectionSecrets(resolver, refs) if refs else None


# ── The hub ───────────────────────────────────────────────────────────


@dataclass
class _Cached:
    checked_at: float
    digest: str
    connectors: ConnectorSet
    connections: Dict[str, Connection]
    errors: Dict[str, str]


class ConnectorHub:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        resolver: SecretResolver,
        *,
        fixed: Optional[ConnectorSet] = None,
        registry: Optional[Registry] = None,
        refresh_seconds: float = REFRESH_SECONDS,
    ) -> None:
        self._db = db
        self._settings = settings
        self._resolver = resolver
        self._fixed = fixed  # demo mode: the mocks, for every tenant
        self._registry = registry or default_registry()
        self._refresh = refresh_seconds
        self._cache: Dict[uuid.UUID, _Cached] = {}

    @property
    def registry(self) -> Registry:
        return self._registry

    async def _connections(self, tenant_id: uuid.UUID) -> Dict[str, Connection]:
        merged = environment_connections(self._settings)
        merged.update(await stored_connections(self._db, tenant_id))
        return merged

    def _build(self, found: Mapping[str, Connection]) -> Tuple[ConnectorSet, Dict[str, str]]:
        demo = self._settings.agent.demo_mode
        built: Dict[str, Connector] = {}
        errors: Dict[str, str] = {}
        for connector_id, connection in found.items():
            cls = self._registry.connector_class(connector_id)
            if cls is None or cls.manifest.family is not Family.OPERATIONS:
                continue
            if not connection.enabled:
                continue
            try:
                built[connector_id] = cls(
                    dict(connection.config),
                    ConnectionSecrets(self._resolver, connection.secret_refs),
                    demo_mode=demo,
                )
            except (ConfigError, SecretError) as exc:
                # One bad connection must not take the others down with it.
                errors[connector_id] = str(exc)
                logger.error("Connection '%s' is not usable: %s", connector_id, exc)
        return ConnectorSet(self._registry, built), errors

    async def _state(self, tenant_id: uuid.UUID) -> _Cached:
        cached = self._cache.get(tenant_id)
        now = time.monotonic()
        if cached and now - cached.checked_at < self._refresh:
            return cached
        found = await self._connections(tenant_id)
        digest = audit.digest(
            {k: [dict(c.config), dict(c.secret_refs), c.enabled] for k, c in found.items()}
        )
        if cached and cached.digest == digest:
            cached.checked_at = now
            return cached
        connectors, errors = self._build(found)
        if cached:
            await cached.connectors.close()
        self._cache[tenant_id] = _Cached(now, digest, connectors, found, errors)
        return self._cache[tenant_id]

    def forget(self, tenant_id: uuid.UUID) -> None:
        """Re-read this tenant's connections on the next use."""
        cached = self._cache.get(tenant_id)
        if cached:
            cached.checked_at = 0.0

    async def for_tenant(self, tenant_id: Optional[uuid.UUID]) -> ConnectorSet:
        if self._fixed is not None:
            return self._fixed
        if tenant_id is None:
            raise ConfigError("Connectors are per tenant; no tenant was given")
        return (await self._state(tenant_id)).connectors

    async def describe(self, tenant_id: uuid.UUID) -> List[Dict[str, Any]]:
        """Every registered connector with this tenant's connection state. No credentials."""
        state = None if self._fixed is not None else await self._state(tenant_id)
        items: List[Dict[str, Any]] = []
        for manifest in self._registry.manifests():
            connection = state.connections.get(manifest.id) if state else None
            if self._fixed is not None:
                configured = self._fixed.connector(manifest.id) is not None
                origin = "demo" if configured else ""
            else:
                configured = connection is not None
                origin = connection.origin if connection else ""
            items.append(
                {
                    **manifest.to_dict(),
                    "configured": configured,
                    "enabled": bool(connection.enabled) if connection else configured,
                    "origin": origin,
                    "error": state.errors.get(manifest.id, "") if state else "",
                    "credentials": dict(connection.secret_refs) if connection else {},
                }
            )
        return items

    async def test(self, tenant_id: uuid.UUID, connector_id: str) -> List[CheckResult]:
        """Run a connector's connection test and report each step."""
        if self._registry.connector_class(connector_id) is None:
            raise LookupError(connector_id)
        connectors = await self.for_tenant(tenant_id)
        connector = connectors.connector(connector_id)
        if connector is None:
            state = self._cache.get(tenant_id)
            reason = (state.errors.get(connector_id) if state else "") or "not configured"
            return [CheckResult("connection", False, reason)]
        return await connector.test_connection()

    async def knowledge_sources(self, tenant_id: uuid.UUID, itsm: Any) -> List[KnowledgeSource]:
        """The SOP sources this tenant has configured."""
        if self._fixed is not None:
            return []
        found = (await self._state(tenant_id)).connections
        sources: List[KnowledgeSource] = []
        kb = found.get("servicenow_kb")
        if kb and kb.enabled and hasattr(itsm, "_get_all"):
            sources.append(
                ServiceNowKnowledge(itsm, kb.config.get("query", "workflow_state=published"))
            )
        git = found.get("git_knowledge")
        if git and git.enabled:
            sources.append(
                GitKnowledge(
                    git.config.get("url", ""),
                    git.config.get("ref", "main") or "main",
                    git.config.get("path", ""),
                    git.config.get("cache_dir", ""),
                    ConnectionSecrets(self._resolver, git.secret_refs),
                    demo_mode=self._settings.agent.demo_mode,
                )
            )
        return sources

    async def close(self) -> None:
        for cached in self._cache.values():
            await cached.connectors.close()
        self._cache.clear()
        if self._fixed is not None:
            await self._fixed.close()
