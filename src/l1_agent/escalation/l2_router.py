"""L2 team routing based on Configuration Item (CI) name.

Loads routing rules from config/l2_routing.json. CI patterns support
fnmatch-style wildcards (e.g. ``DB*``, ``APP*``).
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Dict

from src.l1_agent.utils.logging import get_logger

logger = get_logger("l2_router")

_DEFAULT_ROUTING_PATH = Path(__file__).parent.parent / "config" / "l2_routing.json"


class L2Router:
    """Resolves the correct L2 team for a given CI name."""

    def __init__(self, routing_path: Path = _DEFAULT_ROUTING_PATH) -> None:
        self._routing = self._load(routing_path)

    def _load(self, path: Path) -> Dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load L2 routing config from %s: %s", path, exc)
            return {"default_team": {"name": "L2-General", "email": "l2-general@statestreet.com"}, "ci_mappings": {}}

    def resolve_l2_team(self, ci_name: str) -> Dict[str, str]:
        """Return the L2 team dict for *ci_name*.

        Tries each pattern in ``ci_mappings`` using fnmatch (case-insensitive).
        Falls back to ``default_team`` when no pattern matches or CI is empty.
        """
        ci = (ci_name or "").strip()
        if ci:
            for pattern, team in self._routing.get("ci_mappings", {}).items():
                if fnmatch.fnmatch(ci.upper(), pattern.upper()):
                    logger.debug("CI '%s' matched pattern '%s' -> %s", ci, pattern, team["name"])
                    return dict(team)

        default = self._routing.get("default_team", {"name": "L2-General", "email": "l2-general@statestreet.com"})
        logger.debug("CI '%s' -> default team %s", ci, default["name"])
        return dict(default)
