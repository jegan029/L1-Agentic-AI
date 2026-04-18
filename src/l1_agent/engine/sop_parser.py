"""SOP parser: converts raw ServiceNow SOP records into executable SOP objects."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from src.l1_agent.models.sop import SOP
from src.l1_agent.utils.logging import get_logger

logger = get_logger("sop_parser")


class SOPParser:
    """Parses SOP content from ServiceNow knowledge articles into structured SOP objects.

    Supports two input formats:
    1. JSON-structured SOP (field 'text' or 'sop_content' contains valid JSON)
    2. ServiceNow knowledge article fields mapped to SOP schema
    """

    def parse_from_servicenow(self, record: Dict[str, Any]) -> Optional[SOP]:
        """Parse a ServiceNow knowledge/runbook record into an SOP."""
        try:
            # Try JSON content first
            content = record.get("sop_content") or record.get("text", "")
            if isinstance(content, str) and content.strip().startswith("{"):
                return self._parse_json_sop(content)

            # Fall back to field mapping
            return self._parse_field_mapped(record)
        except Exception as exc:
            logger.error("Failed to parse SOP record: %s", exc)
            return None

    def parse_from_json(self, data: Dict[str, Any]) -> SOP:
        """Parse a pre-structured JSON SOP definition."""
        return SOP.from_dict(data)

    def parse_from_json_string(self, json_str: str) -> Optional[SOP]:
        """Parse a JSON string into an SOP."""
        try:
            data = json.loads(json_str)
            return SOP.from_dict(data)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("Invalid SOP JSON: %s", exc)
            return None

    # ── Internal parsers ──────────────────────────────────────────────

    def _parse_json_sop(self, content: str) -> Optional[SOP]:
        try:
            data = json.loads(content)
            return SOP.from_dict(data)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("JSON SOP parse error: %s", exc)
            return None

    def _parse_field_mapped(self, record: Dict[str, Any]) -> SOP:
        """Map ServiceNow KB article fields to SOP structure."""
        sop_id = record.get("sys_id", record.get("number", ""))
        title = record.get("short_description", record.get("title", ""))
        keywords_raw = record.get("meta", record.get("keywords", ""))
        keywords = (
            [k.strip() for k in keywords_raw.split(",") if k.strip()]
            if isinstance(keywords_raw, str)
            else keywords_raw
        )
        services_raw = record.get("cmdb_ci", record.get("applicable_services", ""))
        services = (
            [s.strip() for s in services_raw.split(",") if s.strip()]
            if isinstance(services_raw, str)
            else services_raw
        )
        categories_raw = record.get("category", "")
        categories = (
            [c.strip() for c in categories_raw.split(",") if c.strip()]
            if isinstance(categories_raw, str)
            else categories_raw
        )

        return SOP(
            sop_id=str(sop_id),
            title=str(title),
            applicable_services=services,
            applicable_categories=categories,
            keywords=keywords,
            steps=[],  # Steps need to be parsed from body content
            version=record.get("version", "1.0"),
        )
