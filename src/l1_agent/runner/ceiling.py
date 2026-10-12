"""What a runner will do at most, whatever the control plane asks (ADR 0010).

The ceiling is set where the runner runs, by the people who run it, from
files and environment variables the control plane cannot reach. It is checked
before a connector is built and before any credential is read.

    max tier        T0 by default: a runner makes no writes until its operator raises it
    capabilities    patterns of capability ids it will carry out
    targets         its own allow lists; a write needs one for every target it names
    references      where a credential reference may point
    connections     connections it holds itself, which replace the control plane's
    kill switch     a file; while it exists the runner makes no write
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.l1_agent.config.settings import ConfigError, RunnerSettings
from src.l1_agent.connectors.sdk.manifest import Capability, Kind

_TIER_RANK = {"T0": 0, "T1": 1, "T2": 2}


def _load_json(path: str, what: str) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{what} could not be read: {type(exc).__name__}") from None
    if not isinstance(loaded, dict):
        raise ConfigError(f"{what} must be a JSON object")
    return loaded


@dataclass(frozen=True)
class LocalConnection:
    config: Mapping[str, Any]
    secret_refs: Mapping[str, str]


@dataclass(frozen=True)
class Ceiling:
    max_tier: str = "T0"
    capabilities: Tuple[str, ...] = ("*",)
    # connector id -> allow list setting -> patterns
    targets: Mapping[str, Mapping[str, Tuple[str, ...]]] = field(default_factory=dict)
    secret_ref_prefixes: Tuple[str, ...] = ()
    connections: Mapping[str, LocalConnection] = field(default_factory=dict)
    kill_switch_file: str = ""

    @classmethod
    def from_settings(cls, settings: RunnerSettings) -> "Ceiling":
        if settings.max_tier not in _TIER_RANK:
            raise ConfigError("RUNNER_MAX_TIER must be T0, T1 or T2")
        targets: Dict[str, Dict[str, Tuple[str, ...]]] = {}
        for connector_id, lists in _load_json(settings.targets_file, "RUNNER_TARGETS_FILE").items():
            if not isinstance(lists, dict):
                raise ConfigError("RUNNER_TARGETS_FILE: each connector maps settings to lists")
            targets[str(connector_id)] = {
                str(key): tuple(str(v) for v in (values or []))
                for key, values in lists.items()
                if isinstance(values, list)
            }
        connections: Dict[str, LocalConnection] = {}
        for connector_id, body in _load_json(
            settings.connections_file, "RUNNER_CONNECTIONS_FILE"
        ).items():
            if not isinstance(body, dict):
                raise ConfigError("RUNNER_CONNECTIONS_FILE: each connector maps to an object")
            config, refs = body.get("config", {}), body.get("secret_refs", {})
            if not isinstance(config, dict) or not isinstance(refs, dict):
                raise ConfigError("RUNNER_CONNECTIONS_FILE: config and secret_refs are objects")
            connections[str(connector_id)] = LocalConnection(
                config, {str(k): str(v) for k, v in refs.items()}
            )
        return cls(
            max_tier=settings.max_tier,
            capabilities=tuple(settings.capabilities) or ("*",),
            targets=targets,
            secret_ref_prefixes=tuple(settings.secret_ref_prefixes),
            connections=connections,
            kill_switch_file=settings.kill_switch_file,
        )

    def kill_switch_on(self) -> bool:
        return bool(self.kill_switch_file) and Path(self.kill_switch_file).exists()

    def describe(self) -> Dict[str, Any]:
        """What the runner tells the control plane about itself. Nothing here is a secret."""
        return {
            "max_tier": self.max_tier,
            "capabilities": list(self.capabilities),
            "kill_switch": self.kill_switch_on(),
            "local_connections": sorted(self.connections),
            "local_targets": {k: sorted(v) for k, v in sorted(self.targets.items())},
        }

    # ── The checks ────────────────────────────────────────────────────

    def refuse_test(self, connector_id: str, secret_refs: Mapping[str, str]) -> Optional[str]:
        """Why a connection test is not made, or None."""
        return self._refuse_references(connector_id, secret_refs)

    def refuse(
        self,
        capability: Capability,
        connector_id: str,
        parameters: Mapping[str, Any],
        secret_refs: Mapping[str, str],
    ) -> Optional[str]:
        """Why this runner will not make the call, or None when it may."""
        if not any(fnmatchcase(capability.id, pattern) for pattern in self.capabilities):
            return f"'{capability.id}' is not a capability this runner carries out"
        tier = capability.tier.value if capability.tier else "T2"
        if _TIER_RANK.get(tier, 99) > _TIER_RANK[self.max_tier]:
            return f"'{capability.id}' is {tier}; this runner goes no higher than {self.max_tier}"
        is_write = capability.kind is Kind.WRITE
        if is_write and self.kill_switch_on():
            return "the runner's kill switch is on"
        problems = self._target_problems(capability, connector_id, parameters, is_write)
        if problems:
            return "; ".join(problems)
        return self._refuse_references(connector_id, secret_refs)

    def _target_problems(
        self,
        capability: Capability,
        connector_id: str,
        parameters: Mapping[str, Any],
        is_write: bool,
    ) -> List[str]:
        local = self.targets.get(connector_id, {})
        problems: List[str] = []
        for binding in capability.allow_lists:
            value = parameters.get(binding.parameter)
            if value in (None, ""):
                continue
            allowed = local.get(binding.config_key)
            if allowed is None:
                if is_write:
                    # The control plane's list is not enough for a change.
                    problems.append(
                        f"this runner has no list of its own for '{binding.config_key}', "
                        "so it makes no write to any target"
                    )
                continue
            if not any(fnmatchcase(str(value), pattern) for pattern in allowed):
                problems.append(
                    f"parameter '{binding.parameter}' is not on this runner's own list "
                    f"'{binding.config_key}'"
                )
        return problems

    def _refuse_references(
        self, connector_id: str, secret_refs: Mapping[str, str]
    ) -> Optional[str]:
        if connector_id in self.connections:
            return None  # the runner's own connection is used; the control plane's is ignored
        for slot, reference in sorted(secret_refs.items()):
            inside = any(str(reference).startswith(prefix) for prefix in self.secret_ref_prefixes)
            if not inside or ".." in str(reference):
                return (
                    f"credential '{slot}' points outside the places this runner reads "
                    "credentials from (RUNNER_SECRET_REF_PREFIXES)"
                )
        return None
