"""Turn articles from knowledge sources into draft SOPs.

Nothing ingested can run. A structured article (a JSON SOP) becomes a draft
with its steps; a prose article becomes a draft with no steps and the
article text alongside, for a person to structure. Either way a person
submits it and a different person approves it before it is published.
"""

from __future__ import annotations

import html
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import sqlalchemy as sa

from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.connectors.knowledge.base import Article, KnowledgeSource
from src.l1_agent.connectors.sdk.manifest import Kind
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.db.models import knowledge_sources
from src.l1_agent.models.sop import SOP, StepType
from src.l1_agent.service.runs import insert_ignoring_conflict
from src.l1_agent.sop import store
from src.l1_agent.sop.validator import validate_sop
from src.l1_agent.utils.logging import get_logger

logger = get_logger("ingest")

INGEST = Actor.system("ingest")
_CODE_RE = re.compile(r"(?is)<\s*(script|style)\b.{0,100000}?<\s*/\s*\1\s*>")
_TAG_RE = re.compile(r"<[^>]{0,2000}>")
_SLUG_RE = re.compile(r"[^A-Za-z0-9_.\-]+")


# Given an article's title, its text and a label for the record, returns drafted
# keywords, parameters and steps, or None.
Drafter = Callable[[str, str, str], Awaitable[Optional[Dict[str, Any]]]]


@dataclass
class SyncResult:
    source_id: str
    articles: int = 0
    drafts: int = 0
    skipped: int = 0
    model_drafted: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source_id,
            "articles": self.articles,
            "drafts": self.drafts,
            "model_drafted": self.model_drafted,
            "skipped": self.skipped,
            "errors": self.errors[:20],
        }


def plain_text(body: str) -> str:
    """Article text with markup removed. The result is shown as text, never as HTML."""
    text = _CODE_RE.sub("", body)  # scripts and styles are not article text
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", text)
    text = html.unescape(_TAG_RE.sub("", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split(value: str) -> List[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def draft_from_article(source_id: str, article: Article) -> Dict[str, Any]:
    """The draft content for an article. A structured article keeps its steps."""
    body = article.body.strip()
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("steps"), list):
            data.setdefault("title", article.title)
            if not data.get("sop_id"):
                data["sop_id"] = _key(source_id, article)
            return data
    meta = article.metadata
    return {
        "sop_id": _key(source_id, article),
        "title": article.title,
        "keywords": _split(meta.get("keywords", "")),
        "applicable_categories": _split(meta.get("category", "")),
        "applicable_services": _split(meta.get("cmdb_ci", "")),
        "steps": [],  # a person writes the steps
    }


def read_capabilities(registry: Any) -> List[Dict[str, Any]]:
    """The read capabilities a drafted step may name, as a model is shown them."""
    listed = []
    for capability_id in registry.capability_ids():
        capability = registry.capability(capability_id)
        if capability.kind != Kind.READ:
            continue
        properties = capability.parameters.get("properties", {})
        listed.append(
            {
                "id": capability.id,
                "summary": capability.summary,
                "parameters": sorted(properties),
                "required": list(capability.parameters.get("required", [])),
            }
        )
    return listed


def model_drafter(gateway: Any, tenant_id: uuid.UUID, registry: Any) -> Optional[Drafter]:
    """A drafter that asks the tenant's model, or None when there is no gateway."""
    if gateway is None or gateway.provider is None:
        return None
    capabilities = read_capabilities(registry)

    async def draft(title: str, text: str, subject: str) -> Optional[Dict[str, Any]]:
        return await gateway.advisor(tenant_id, None, subject).draft_sop(title, text, capabilities)

    return draft


def accept_model_draft(
    content: Dict[str, Any], drafted: Optional[Dict[str, Any]], registry: Any
) -> Optional[Dict[str, Any]]:
    """The article's draft with the model's steps, or None when they cannot be kept.

    They are kept only if the result passes the SOP validator and changes
    nothing: no ACTION step, no write capability and no change template.
    """
    if not drafted or not drafted.get("steps"):
        return None
    merged = {**content, "steps": drafted["steps"], "parameters": drafted.get("parameters", [])}
    if drafted.get("keywords") and not merged.get("keywords"):
        merged["keywords"] = drafted["keywords"]
    merged.pop("change_template", None)
    try:
        sop = SOP.from_dict(merged)
        if validate_sop(sop):
            return None
    except Exception:
        return None
    for step in sop.steps:
        if step.step_type.upper() == StepType.ACTION.value:
            return None
        named = step.capability or str(step.parameters.get("check", ""))
        capability = registry.capability(named) if named else None
        if capability is not None and capability.kind != Kind.READ:
            return None
    return merged


def _key(source_id: str, article: Article) -> str:
    name = article.metadata.get("number") or article.id
    return _SLUG_RE.sub("-", f"{source_id}-{name}").strip("-.")[:100] or f"{source_id}-article"


async def _cursor(db: Database, tenant_id: uuid.UUID, source_id: str) -> str:
    async with db.tenant(tenant_id) as conn:
        found = await conn.execute(
            sa.select(knowledge_sources.c.cursor).where(
                knowledge_sources.c.tenant_id == tenant_id,
                knowledge_sources.c.source_id == source_id,
            )
        )
        return found.scalar() or ""


async def _record(
    db: Database, tenant_id: uuid.UUID, actor: Actor, result: SyncResult, cursor: Optional[str]
) -> None:
    values: Dict[str, Any] = {
        "last_synced_at": utcnow(),
        "last_error": "; ".join(result.errors)[:2000],
    }
    if cursor is not None:
        values["cursor"] = cursor[:200]
    async with db.tenant(tenant_id) as conn:
        await conn.execute(
            insert_ignoring_conflict(conn, knowledge_sources).values(
                tenant_id=tenant_id, source_id=result.source_id
            )
        )
        await conn.execute(
            sa.update(knowledge_sources)
            .where(
                knowledge_sources.c.tenant_id == tenant_id,
                knowledge_sources.c.source_id == result.source_id,
            )
            .values(**values)
        )
        await audit.append(conn, tenant_id, actor, "knowledge.synced", result.to_dict())


async def _already_ingested(
    db: Database, tenant_id: uuid.UUID, source_id: str, article: Article
) -> bool:
    from src.l1_agent.db.models import sop_versions

    async with db.tenant(tenant_id) as conn:
        found = await conn.execute(
            sa.select(sop_versions.c.id)
            .where(
                sop_versions.c.tenant_id == tenant_id,
                sop_versions.c.source_id == source_id,
                sop_versions.c.source_article == article.id,
                sop_versions.c.source_revision == article.revision,
            )
            .limit(1)
        )
        return found.first() is not None


async def sync_source(
    db: Database,
    tenant_id: uuid.UUID,
    source: KnowledgeSource,
    actor: Actor = INGEST,
    drafter: Optional[Drafter] = None,
    registry: Any = None,
) -> SyncResult:
    """Read what changed in one source and add a draft for each new article revision.

    With a `drafter`, the steps of a prose article are drafted by a model and
    kept if they pass the validator; the draft is then marked as model-drafted.
    """
    result = SyncResult(source.source_id)
    try:
        articles, cursor = await source.changed_since(
            await _cursor(db, tenant_id, source.source_id)
        )
    except Exception as exc:
        logger.error("Knowledge source %s could not be read: %s", source.source_id, exc)
        result.errors.append(f"could not read the source: {type(exc).__name__}")
        await _record(db, tenant_id, actor, result, None)
        return result

    result.articles = len(articles)
    for article in articles:
        content = draft_from_article(source.source_id, article)
        text = plain_text(article.body)
        origin = store.ORIGIN_INGESTED
        if (
            drafter is not None
            and registry is not None
            and not content.get("steps")
            # A revision that is already a draft is not sent to a model again.
            and not await _already_ingested(db, tenant_id, source.source_id, article)
        ):
            try:
                drafted = await drafter(article.title, text, f"{source.source_id}/{article.id}")
            except Exception as exc:
                logger.warning("Model draft unavailable: %s", type(exc).__name__)
                drafted = None
            accepted = accept_model_draft(content, drafted, registry)
            if accepted is not None:
                content, origin = accepted, store.ORIGIN_MODEL_DRAFTED
        try:
            created = await store.create_draft(
                db,
                tenant_id,
                INGEST,
                content,
                origin=origin,
                source=store.Source(source.source_id, article.id, article.revision),
                source_text=text,
            )
        except store.SopError as exc:
            result.errors.append(f"{article.id}: {exc}")
            continue
        if created is None:
            result.skipped += 1
        else:
            result.drafts += 1
            result.model_drafted += origin == store.ORIGIN_MODEL_DRAFTED
    # The cursor moves on even past an article that could not be turned into a draft:
    # the error is on record, and the article comes back when it is next edited.
    await _record(db, tenant_id, actor, result, cursor)
    return result


async def sync_all(
    db: Database,
    tenant_id: uuid.UUID,
    sources: List[KnowledgeSource],
    actor: Actor = INGEST,
    drafter: Optional[Drafter] = None,
    registry: Any = None,
) -> List[SyncResult]:
    return [
        await sync_source(db, tenant_id, source, actor, drafter, registry) for source in sources
    ]
