"""IBM MQ, read through IR360: queue depth, status and browse."""

from __future__ import annotations

from typing import Mapping

from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.adapters.ir360_adapter import IR360Adapter
from src.l1_agent.config.settings import IR360Settings
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


class IbmMqConnector(AdapterConnector):
    manifest = Manifest(
        id="ibm_mq",
        version="1.0.0",
        family=Family.OPERATIONS,
        display_name="IBM MQ (IR360)",
        config=closed(
            {
                "base_url": {"type": "string"},
                "verify_ssl": {"type": "boolean"},
                "use_cli": {"type": "boolean"},
                "cli_path": {"type": "string"},
            }
        ),
        secrets=(SecretSlot("api_key", "IR360 API key (not used in CLI mode)", required=False),),
        capabilities=(
            Capability(
                id="ibm_mq.queue.check",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Queue depth, queue manager status, or a non-destructive browse",
                parameters=closed(
                    {
                        "queue_manager": {"type": "string", "maxLength": 48},
                        "queue": {"type": "string", "maxLength": 48},
                        "action": {"type": "string", "enum": ["depth", "browse", "status"]},
                    },
                    required=["queue_manager"],
                ),
                scopes=("queue.inquire", "queue.browse"),
                retryable_errors=("timeout", "unavailable"),
                timeout_seconds=30,
            ),
        ),
    )

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        return IR360Adapter(
            IR360Settings(
                base_url=self.config.get("base_url", ""),
                api_key=secrets.get("api_key", ""),
                cli_path=self.config.get("cli_path", ""),
                use_cli=bool(self.config.get("use_cli", False)),
                verify_ssl=tls_setting(self.config, self.demo_mode),
            )
        )
