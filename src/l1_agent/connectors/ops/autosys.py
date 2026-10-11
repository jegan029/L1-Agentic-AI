"""Autosys: job status and dependencies, read only.

The force start arrives in Phase 4 as a write capability behind the policy
engine. Nothing here can change a job.
"""

from __future__ import annotations

from typing import Mapping

from src.l1_agent.adapters.autosys_adapter import AutosysAdapter
from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.config.settings import AutosysSettings
from src.l1_agent.connectors.sdk.base import AdapterConnector
from src.l1_agent.connectors.sdk.manifest import Capability, Family, Kind, Manifest, RiskTier
from src.l1_agent.connectors.sdk.schema import closed


class AutosysConnector(AdapterConnector):
    manifest = Manifest(
        id="autosys",
        version="1.0.0",
        family=Family.OPERATIONS,
        display_name="Autosys",
        config=closed(
            {
                "cli_path": {"type": "string"},
                "allowed_commands": {"type": "array"},
            }
        ),
        capabilities=(
            Capability(
                id="autosys.job.status",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Status of a job or box (autorep), or its dependencies (job_depends)",
                parameters=closed(
                    {
                        "job_name": {"type": "string", "maxLength": 64},
                        "query_type": {"type": "string", "enum": ["status", "dependencies"]},
                    },
                    required=["job_name"],
                ),
                scopes=("autorep", "job_depends"),
                target_parameters=("job_name",),
                timeout_seconds=30,
            ),
        ),
    )

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        return AutosysAdapter(
            AutosysSettings(
                cli_path=self.config.get("cli_path", "autorep"),
                allowed_commands=tuple(self.config.get("allowed_commands") or ("autorep",)),
            )
        )
