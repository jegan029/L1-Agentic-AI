"""ServiceNow knowledge base as an SOP source."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from src.l1_agent.connectors.itsm.servicenow import ServiceNowConnector
from src.l1_agent.connectors.knowledge.base import MAX_ARTICLE_CHARS, Article
from src.l1_agent.connectors.sdk.manifest import Capability, Family, Kind, Manifest, RiskTier
from src.l1_agent.connectors.sdk.schema import closed
from src.l1_agent.models.incident import _display_value, _raw_value

# ServiceNow timestamps: "2026-01-31 09:15:00". Anything else is not sent in a query.
_CURSOR_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_SYS_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# A filter is an encoded query written by a tenant administrator, not ticket text.
_QUERY_RE = re.compile(r"^[A-Za-z0-9_.=^!<>@ ,:\-]*$")
FIELDS = "sys_id,number,short_description,text,sys_updated_on,sys_mod_count,meta,category,cmdb_ci"


class ServiceNowKnowledge:
    manifest = Manifest(
        id="servicenow_kb",
        version="1.0.0",
        family=Family.KNOWLEDGE,
        display_name="ServiceNow knowledge base",
        notes="The integration user needs read on kb_knowledge (the knowledge role).",
        config=closed({"query": {"type": "string"}, "table": {"type": "string"}}),
        capabilities=(
            Capability(
                id="servicenow_kb.articles.list",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Published articles changed since a point in time",
                parameters=closed({"cursor": {"type": "string", "maxLength": 19}}),
                scopes=("kb_knowledge.read",),
            ),
        ),
    )
    source_id = "servicenow_kb"

    def __init__(self, client: ServiceNowConnector, query: str = "workflow_state=published"):
        if not _QUERY_RE.match(query):
            raise ValueError("The knowledge base filter contains characters that are not allowed")
        self._client = client
        self._query = query

    @staticmethod
    def _article(record: Dict[str, Any]) -> Article:
        sys_id = _raw_value(record.get("sys_id", ""))
        updated = _raw_value(record.get("sys_updated_on", ""))
        return Article(
            id=sys_id,
            title=_display_value(record.get("short_description", "")),
            body=_display_value(record.get("text", ""))[:MAX_ARTICLE_CHARS],
            revision=f"{_raw_value(record.get('sys_mod_count', ''))}@{updated}",
            updated_at=updated,
            metadata={
                "number": _display_value(record.get("number", "")),
                "keywords": _display_value(record.get("meta", "")),
                "category": _display_value(record.get("category", "")),
                "cmdb_ci": _display_value(record.get("cmdb_ci", "")),
            },
        )

    async def changed_since(self, cursor: str) -> Tuple[List[Article], str]:
        if cursor and not _CURSOR_RE.match(cursor):
            raise ValueError("The knowledge base cursor is not a ServiceNow timestamp")
        parts = [p for p in (self._query, f"sys_updated_on>{cursor}" if cursor else "") if p]
        rows = await self._client._get_all(
            f"/api/now/table/{self._client._settings.sop_table}",
            {
                "sysparm_query": "^".join(parts) + "^ORDERBYsys_updated_on",
                "sysparm_display_value": "all",
                "sysparm_fields": FIELDS,
            },
        )
        articles = [self._article(r) for r in rows]
        articles = [a for a in articles if a.id]
        newest = max((a.updated_at for a in articles if a.updated_at), default=cursor)
        return articles, newest

    async def fetch(self, article_id: str) -> Optional[Article]:
        if not _SYS_ID_RE.match(article_id):
            return None
        record = (
            await self._client._get(
                f"/api/now/table/{self._client._settings.sop_table}/{article_id}",
                {"sysparm_display_value": "all", "sysparm_fields": FIELDS},
            )
        ).get("result", {})
        return self._article(record) if record else None

    async def close(self) -> None:
        return None  # the ITSM connector owns the HTTP session
