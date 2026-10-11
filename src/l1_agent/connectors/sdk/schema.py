"""Small helpers for writing manifests."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

from src.l1_agent.config.settings import ConfigError


def closed(properties: Mapping[str, Any], required: Iterable[str] = ()) -> Dict[str, Any]:
    """A JSON Schema object that accepts only the listed properties."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(required),
    }


def tls_setting(config: Mapping[str, Any], demo_mode: bool, key: str = "verify_ssl") -> bool:
    """TLS verification is on. A connection may switch it off only in demo mode."""
    if config.get(key, True):
        return True
    if not demo_mode:
        raise ConfigError(f"{key}=false is only allowed in demo mode")
    return False
