"""File share: read the tail of a log file under an allowed path."""

from __future__ import annotations

from typing import Mapping

from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.adapters.windows_share_adapter import WindowsShareAdapter
from src.l1_agent.config.settings import WindowsShareSettings
from src.l1_agent.connectors.sdk.base import AdapterConnector
from src.l1_agent.connectors.sdk.manifest import (
    AllowListBinding,
    Capability,
    Family,
    Kind,
    Manifest,
    RiskTier,
    SecretSlot,
)
from src.l1_agent.connectors.sdk.schema import closed


class FileShareConnector(AdapterConnector):
    manifest = Manifest(
        id="fileshare",
        version="1.0.0",
        family=Family.OPERATIONS,
        display_name="File share",
        config=closed(
            {
                "allowed_unc_prefixes": {"type": "array"},
                "smb_username": {"type": "string"},
                "smb_domain": {"type": "string"},
            }
        ),
        secrets=(SecretSlot("smb_password", "Password of the read-only share user", False),),
        capabilities=(
            Capability(
                id="fileshare.file.read",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Last lines of a file, optionally filtered by a fixed text pattern",
                parameters=closed(
                    {
                        "unc_path": {"type": "string", "maxLength": 1024},
                        "pattern": {"type": "string", "maxLength": 200},
                        "last_n_lines": {"type": "integer", "minimum": 1, "maximum": 5000},
                    },
                    required=["unc_path"],
                ),
                scopes=("share.read",),
                # The adapter compares by path segment; the SDK refuses an empty list first.
                allow_lists=(AllowListBinding("unc_path", "allowed_unc_prefixes", "custom"),),
                target_parameters=("unc_path",),
                timeout_seconds=30,
            ),
        ),
    )

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        return WindowsShareAdapter(
            WindowsShareSettings(
                allowed_unc_prefixes=list(self.config.get("allowed_unc_prefixes") or []),
                smb_username=self.config.get("smb_username", ""),
                smb_password=secrets.get("smb_password", ""),
                smb_domain=self.config.get("smb_domain", ""),
            )
        )
