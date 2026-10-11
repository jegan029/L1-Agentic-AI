"""Mainframe (TN3270): read a status screen through a stored navigation script.

Preview. The keystrokes come from the connection's `default_navigation`, set
by a tenant administrator; a step can choose a transaction from the allow
list but can never supply keystrokes.
"""

from __future__ import annotations

from typing import Mapping

from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.adapters.mainframe_adapter import MainframeAdapter
from src.l1_agent.config.settings import ConfigError, MainframeSettings
from src.l1_agent.connectors.sdk.base import AdapterConnector
from src.l1_agent.connectors.sdk.manifest import (
    AllowListBinding,
    Capability,
    Family,
    Kind,
    Manifest,
    RiskTier,
)
from src.l1_agent.connectors.sdk.schema import closed


class MainframeConnector(AdapterConnector):
    manifest = Manifest(
        id="mainframe",
        version="0.9.0",
        family=Family.OPERATIONS,
        display_name="Mainframe (TN3270)",
        preview=True,
        notes="Needs the x3270 emulator; not installed in the default image.",
        config=closed(
            {
                "host": {"type": "string"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "use_tls": {"type": "boolean"},
                "allowed_transactions": {"type": "array"},
                "default_navigation": {"type": "array"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
            }
        ),
        capabilities=(
            Capability(
                id="mainframe.screen.check",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Read a job or transaction status screen",
                parameters=closed(
                    {
                        "action": {
                            "type": "string",
                            "enum": ["async_status", "job_status", "screen_check"],
                        },
                        "job_name": {"type": "string", "maxLength": 64},
                        "transaction": {"type": "string", "maxLength": 8},
                        "expected_status": {"type": "string", "maxLength": 64},
                        "expected_text": {"type": "string", "maxLength": 200},
                    }
                ),
                scopes=("tn3270.signon", "transaction.inquire"),
                allow_lists=(AllowListBinding("transaction", "allowed_transactions", "custom"),),
                target_parameters=("transaction",),
                timeout_seconds=60,
            ),
        ),
    )

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        use_tls = bool(self.config.get("use_tls", True))
        if not use_tls and not self.demo_mode:
            raise ConfigError("use_tls=false is only allowed in demo mode")
        return MainframeAdapter(
            MainframeSettings(
                host=self.config.get("host", ""),
                port=int(self.config.get("port", 992)),
                use_tls=use_tls,
                allowed_transactions=list(self.config.get("allowed_transactions") or []),
                default_navigation=list(self.config.get("default_navigation") or []),
                timeout_seconds=int(self.config.get("timeout_seconds", 30)),
            )
        )
