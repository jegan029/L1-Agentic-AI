"""Closed parameter schemas for check steps.

The schemas are the ones each connector declares in its manifest
(connectors/sdk/manifest.py); this module looks them up by step type or by
capability id. Every call is validated before the connector runs, and a key
that is not listed is rejected, so a caller cannot pass options a connector
happens to read (for example mainframe keystrokes).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from src.l1_agent.connectors.catalog import (
    ADAPTER_CONNECTORS,
    STEP_CAPABILITIES,
    default_registry,
)
from src.l1_agent.connectors.sdk.manifest import Capability, Family, validate_parameters

# step type -> original adapter key
STEP_ADAPTERS: Dict[str, str] = {
    step_type: next(
        key
        for key, connector in ADAPTER_CONNECTORS.items()
        if connector == default_registry().owner(capability)
    )
    for step_type, capability in STEP_CAPABILITIES.items()
}

# step type -> the JSON Schema of its parameters
STEP_PARAMETER_SCHEMAS: Dict[str, Mapping[str, Any]] = {
    step_type: default_registry().capability(capability).parameters
    for step_type, capability in STEP_CAPABILITIES.items()
}


def find_capability(name: str) -> Optional[Capability]:
    """The capability a legacy step type or a capability id refers to."""
    capability_id = STEP_CAPABILITIES.get((name or "").upper(), name)
    return default_registry().capability(capability_id)


def is_step_capability(capability_id: str) -> bool:
    """True for a capability of an operations connector, the only kind a step may call."""
    registry = default_registry()
    owner = registry.connector_class(registry.owner(capability_id) or "")
    return owner is not None and owner.manifest.family is Family.OPERATIONS


def validate_step_parameters(
    step_type: str,
    parameters: Any,
    *,
    allow_placeholders: bool = False,
) -> List[str]:
    """Return a list of problems; an empty list means the parameters are valid.

    ``step_type`` is a legacy step type or a capability id.
    ``allow_placeholders`` is for static validation of an SOP, where a value
    may still be an unrendered ``${name}``.
    """
    capability = find_capability(step_type)
    if capability is None:
        return [f"no parameter schema for step type '{step_type}'"]
    return validate_parameters(
        capability.parameters,
        parameters,
        subject=step_type,
        allow_placeholders=allow_placeholders,
    )
