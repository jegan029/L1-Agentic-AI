"""Dynatrace: host health, problems and metrics."""

from __future__ import annotations

from typing import Mapping

from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.adapters.dynatrace_adapter import DynatraceAdapter
from src.l1_agent.config.settings import DynatraceSettings
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

_PARAMETERS = closed(
    {
        "action": {"type": "string", "enum": ["vm_health", "metrics", "problems"]},
        "host_name": {"type": "string", "maxLength": 253},
        "host_group": {"type": "string", "maxLength": 253},
        "metric_selector": {"type": "string", "maxLength": 2000},
        "entity_selector": {"type": "string", "maxLength": 2000},
        "time_range": {"type": "string", "maxLength": 64},
    }
)


class DynatraceConnector(AdapterConnector):
    manifest = Manifest(
        id="dynatrace",
        version="1.0.0",
        family=Family.OPERATIONS,
        display_name="Dynatrace",
        config=closed(
            {
                "base_url": {"type": "string"},
                "verify_ssl": {"type": "boolean"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
            },
            required=["base_url"],
        ),
        secrets=(SecretSlot("api_token", "Dynatrace API token with the read scopes below"),),
        capabilities=(
            Capability(
                id="dynatrace.host.health",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Host state and open problems for a host or host group",
                parameters=_PARAMETERS,
                scopes=("entities.read", "problems.read"),
                retryable_errors=("timeout", "unavailable"),
                timeout_seconds=30,
            ),
            Capability(
                id="dynatrace.metrics.query",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Query metrics such as CPU and memory for an entity",
                parameters=_PARAMETERS,
                scopes=("metrics.read",),
                retryable_errors=("timeout", "unavailable"),
                timeout_seconds=30,
            ),
        ),
    )

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        return DynatraceAdapter(
            DynatraceSettings(
                base_url=self.config.get("base_url", ""),
                api_token=secrets.get("api_token", ""),
                verify_ssl=tls_setting(self.config, self.demo_mode),
                timeout_seconds=int(self.config.get("timeout_seconds", 30)),
            )
        )
