"""Splunk: log search."""

from __future__ import annotations

from typing import Mapping

from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.adapters.splunk_adapter import SplunkAdapter
from src.l1_agent.config.settings import SplunkSettings
from src.l1_agent.connectors.sdk.base import AdapterConnector
from src.l1_agent.connectors.sdk.manifest import (
    Capability,
    Family,
    Kind,
    Manifest,
    RiskTier,
    SecretSlot,
)
from src.l1_agent.connectors.sdk.schema import closed, tls_setting


class SplunkConnector(AdapterConnector):
    manifest = Manifest(
        id="splunk",
        version="1.0.0",
        family=Family.OPERATIONS,
        display_name="Splunk",
        config=closed(
            {
                "base_url": {"type": "string"},
                "verify_ssl": {"type": "boolean"},
                "search_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
            },
            required=["base_url"],
        ),
        secrets=(SecretSlot("token", "Splunk authentication token for the search user"),),
        capabilities=(
            Capability(
                id="splunk.search",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Run a search and return up to the first results",
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
                retryable_errors=("timeout", "unavailable"),
                timeout_seconds=120,
            ),
        ),
    )

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        return SplunkAdapter(
            SplunkSettings(
                base_url=self.config.get("base_url", ""),
                token=secrets.get("token", ""),
                verify_ssl=tls_setting(self.config, self.demo_mode),
                search_timeout_seconds=int(self.config.get("search_timeout_seconds", 120)),
            )
        )
