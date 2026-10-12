"""Splunk: log search, through the Splunk MCP Server app or the REST search API.

The backend is a connection setting. Both sit behind one guard
(`adapters/splunk/guard.py`), so what may be searched and what a result looks
like do not depend on which is configured.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.adapters.splunk.adapter import SplunkSearchAdapter
from src.l1_agent.adapters.splunk.guard import GuardConfig, parse_duration
from src.l1_agent.adapters.splunk.mcp_backend import DEFAULT_SEARCH_TOOL, McpBackend
from src.l1_agent.adapters.splunk.rest_backend import RestBackend
from src.l1_agent.config.settings import ConfigError
from src.l1_agent.connectors.sdk.base import AdapterConnector, Health, HealthStatus
from src.l1_agent.connectors.sdk.manifest import (
    AllowListBinding,
    Capability,
    Family,
    Kind,
    Manifest,
    RiskTier,
    SecretSlot,
)
from src.l1_agent.connectors.sdk.schema import closed, tls_setting
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets, SecretError

BACKENDS = ("rest", "mcp")
DEFAULT_MAX_LOOKBACK = "24h"
DEFAULT_MAX_RESULTS = 100


class SplunkConnector(AdapterConnector):
    manifest = Manifest(
        id="splunk",
        version="2.0.0",
        family=Family.OPERATIONS,
        display_name="Splunk",
        config=closed(
            {
                "backend": {"type": "string", "enum": list(BACKENDS)},
                "base_url": {"type": "string"},
                "mcp_url": {"type": "string"},
                "mcp_search_tool": {"type": "string", "pattern": r"[A-Za-z0-9_.\-]{1,100}"},
                "verify_ssl": {"type": "boolean"},
                "ca_bundle": {"type": "string"},
                "search_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
                "allowed_indexes": {"type": "array"},
                "max_lookback": {"type": "string", "maxLength": 20},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
        ),
        secrets=(
            SecretSlot(
                "token", "Splunk authentication token for the search user (REST)", required=False
            ),
            SecretSlot(
                "mcp_token",
                "Token issued by the Splunk MCP Server app, with or without 'Bearer '",
                required=False,
            ),
        ),
        capabilities=(
            Capability(
                id="splunk.search",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Run a guarded, read-only search and return up to the first results",
                parameters=closed(
                    {
                        "query": {"type": "string", "maxLength": 4000},
                        "time_range": {"type": "object"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                    required=["query"],
                ),
                output=closed({"result_count": {"type": "integer"}, "results": {"type": "array"}}),
                scopes=("search",),
                # The guard compares every index the query names with the list.
                allow_lists=(AllowListBinding("query", "allowed_indexes", "custom"),),
                retryable_errors=("timeout", "unavailable"),
                timeout_seconds=120,
            ),
        ),
        notes=(
            "A search must begin with 'search' or 'index=', name only allow-listed indexes "
            "and use only read-only commands. The guard is pattern matching, not an SPL "
            "parser; the search user's Splunk role is what limits access."
        ),
    )

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        secrets: Optional[ConnectionSecrets] = None,
        *,
        demo_mode: bool = False,
        fixed: Optional[BaseAdapter] = None,
    ) -> None:
        super().__init__(config, secrets, demo_mode=demo_mode, fixed=fixed)
        if fixed is None:
            # A connection that cannot work is refused when it is read, not at the first search.
            self.check_config(self.config)
            tls_setting(self.config, self.demo_mode)

    @classmethod
    def check_config(cls, config: Mapping[str, Any]) -> None:
        cls._backend_of(config)
        cls._guard_of(config)

    @staticmethod
    def _backend_of(config: Mapping[str, Any]) -> str:
        """The configured backend. With none named: MCP when only its URL is given."""
        only_mcp = config.get("mcp_url") and not config.get("base_url")
        name = config.get("backend") or ("mcp" if only_mcp else "rest")
        url_key = "mcp_url" if name == "mcp" else "base_url"
        if not config.get(url_key):
            raise ConfigError(f"The Splunk {name} backend needs '{url_key}'")
        return name

    @staticmethod
    def _guard_of(config: Mapping[str, Any]) -> GuardConfig:
        indexes = config.get("allowed_indexes") or []
        if any(not isinstance(i, str) or not i or "*" in i for i in indexes):
            raise ConfigError("allowed_indexes must be index names, without wildcards")
        try:
            lookback = parse_duration(config.get("max_lookback") or DEFAULT_MAX_LOOKBACK)
        except ValueError as exc:
            raise ConfigError(f"max_lookback: {exc}") from None
        return GuardConfig(
            tuple(indexes), lookback, int(config.get("max_results") or DEFAULT_MAX_RESULTS)
        )

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        common = {
            "verify_ssl": tls_setting(self.config, self.demo_mode),
            "ca_bundle": self.config.get("ca_bundle", ""),
            "timeout_seconds": int(self.config.get("search_timeout_seconds", 120)),
        }
        if self._backend_of(self.config) == "mcp":
            if not secrets.get("mcp_token"):
                raise SecretError("the Splunk MCP backend needs the 'mcp_token' credential")
            backend: Any = McpBackend(
                self.config["mcp_url"],
                secrets["mcp_token"],
                self.config.get("mcp_search_tool") or DEFAULT_SEARCH_TOOL,
                **common,
            )
        else:
            backend = RestBackend(self.config["base_url"], secrets.get("token", ""), **common)
        return SplunkSearchAdapter(backend, self._guard_of(self.config))

    async def health(self) -> Health:
        if self._fixed is not None:
            return await super().health()
        try:
            adapter = await self._adapter()
            ok, reason = await adapter.health()  # type: ignore[attr-defined]
        except SecretError as exc:
            return Health(HealthStatus.UNAVAILABLE, f"credential unavailable: {exc}")
        except Exception as exc:
            return Health(HealthStatus.UNAVAILABLE, type(exc).__name__)
        return Health(HealthStatus.HEALTHY) if ok else Health(HealthStatus.UNAVAILABLE, reason)
