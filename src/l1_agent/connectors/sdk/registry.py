"""The connector registry (ADR 0005).

The registry is the only source of connectors and capabilities. It lints
every manifest when it is built and refuses to exist if one has a problem,
so a service cannot start with a connector that fails the rules.

Only first-party connectors are loaded: the list is in `catalog.py`.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Type

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.connectors.sdk.base import AdapterConnector, Connector, StepContext
from src.l1_agent.connectors.sdk.manifest import Capability, Manifest, lint
from src.l1_agent.utils.logging import get_logger

logger = get_logger("registry")


class ManifestError(ValueError):
    """A connector's manifest breaks a rule; the registry is not built."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = problems
        super().__init__("Connector manifests are not valid: " + "; ".join(problems))


class Registry:
    def __init__(self, connectors: Iterable[type]) -> None:
        self._classes: Dict[str, type] = {}
        self._capabilities: Dict[str, Tuple[str, Capability]] = {}
        problems: List[str] = []
        classes = list(connectors)
        for cls in classes:
            manifest: Manifest = cls.manifest
            if manifest.id in self._classes:
                problems.append(f"connector '{manifest.id}' is registered twice")
                continue
            self._classes[manifest.id] = cls
            for capability in manifest.capabilities:
                if capability.id in self._capabilities:
                    problems.append(f"capability '{capability.id}' is declared by two connectors")
                self._capabilities[capability.id] = (manifest.id, capability)
        known = frozenset(self._capabilities)
        for cls in classes:
            problems += lint(cls.manifest, known)
        if problems:
            raise ManifestError(problems)

    def manifests(self) -> List[Manifest]:
        return [cls.manifest for _, cls in sorted(self._classes.items())]

    def connector_class(self, connector_id: str) -> Optional[type]:
        return self._classes.get(connector_id)

    def capability(self, capability_id: str) -> Optional[Capability]:
        found = self._capabilities.get(capability_id)
        return found[1] if found else None

    def owner(self, capability_id: str) -> Optional[str]:
        """The id of the connector that declares a capability."""
        found = self._capabilities.get(capability_id)
        return found[0] if found else None

    def capability_ids(self) -> List[str]:
        return sorted(self._capabilities)


class ConnectorSet:
    """The connectors one tenant has configured. The engine invokes capabilities through it."""

    def __init__(self, registry: Registry, connectors: Mapping[str, Connector]) -> None:
        self._registry = registry
        self._connectors = dict(connectors)

    @property
    def registry(self) -> Registry:
        return self._registry

    def connector(self, connector_id: str) -> Optional[Connector]:
        return self._connectors.get(connector_id)

    def connectors(self) -> Dict[str, Connector]:
        return dict(self._connectors)

    def tool_name(self, capability_id: str) -> str:
        owner = self._registry.owner(capability_id)
        connector = self._connectors.get(owner or "")
        return connector.tool_name if connector else (owner or "none")

    def _owner(self, capability_id: str) -> Connector:
        owner = self._registry.owner(capability_id)
        if owner is None:
            raise LookupError(f"Unknown capability '{capability_id}'")
        connector = self._connectors.get(owner)
        if connector is None:
            raise LookupError(f"Connector '{owner}' is not configured")
        return connector

    def refusals(self, capability_id: str, parameters: Mapping[str, Any]) -> List[str]:
        """Why a call cannot be made at all: unknown, not configured, or refused parameters."""
        try:
            return self._owner(capability_id).refusals(capability_id, parameters)
        except LookupError as exc:
            return [str(exc)]

    async def invoke(
        self, capability_id: str, parameters: Mapping[str, Any], context: Optional[StepContext]
    ) -> AdapterResult:
        return await self._owner(capability_id).invoke(capability_id, parameters, context)

    async def close(self) -> None:
        for connector in self._connectors.values():
            try:
                await connector.close()
            except Exception as exc:
                logger.warning("Closing %s failed: %s", connector.id, type(exc).__name__)


# Values that satisfy "an empty allow list denies" for the mocks, which have no real targets.
_MOCK_ALLOW = ["mock"]


def connectors_from_adapters(
    registry: Registry,
    adapters: Mapping[str, BaseAdapter],
    adapter_keys: Mapping[str, str],
) -> ConnectorSet:
    """Wrap ready adapters (the mocks, or a test's fakes) as connectors.

    `adapter_keys` maps the original adapter key ("ir360") to a connector id ("ibm_mq").
    """
    built: Dict[str, Connector] = {}
    for key, adapter in adapters.items():
        connector_id = adapter_keys.get(key, key)
        cls: Optional[Type[AdapterConnector]] = registry.connector_class(connector_id)
        if cls is None:
            raise LookupError(f"No connector is registered for adapter '{key}'")
        allow = {
            binding.config_key: list(_MOCK_ALLOW)
            for capability in cls.manifest.capabilities
            for binding in capability.allow_lists
        }
        built[connector_id] = cls(allow, demo_mode=True, fixed=adapter)
    return ConnectorSet(registry, built)
