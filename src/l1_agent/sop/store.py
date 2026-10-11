"""SOP versions and their lifecycle (docs/target-architecture.md section 5).

    draft -> in_review -> approved -> published -> superseded | retired
               |  ^
               +--+ changes requested

Only a published version is matched or run, and the engine reads through the
`published_sops` view. A version's content is hashed when it is submitted;
the approver approves that hash, and from then on the content cannot change.
The approver is never the author: the service checks it and so does a
database constraint. Every transition writes its audit event in the same
transaction.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from src.l1_agent.adapters.schemas import find_capability
from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.connectors.catalog import default_registry
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.db.models import (
    SOP_APPROVED,
    SOP_DRAFT,
    SOP_IN_REVIEW,
    SOP_PUBLISHED,
    SOP_RETIRED,
    SOP_SUPERSEDED,
    published_sops,
    sop_versions,
    sops,
)
from src.l1_agent.models.sop import CONNECTOR_STEP_TYPES, SOP, SOP_STATUS_PUBLISHED, StepType
from src.l1_agent.sop.validator import validate_sop

ORIGIN_AUTHORED = "authored"
ORIGIN_INGESTED = "ingested"
ORIGIN_SEED = "seed"
MAX_SOURCE_TEXT = 200_000
MAX_NOTE = 2000
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")


class SopError(Exception):
    """A lifecycle request that cannot be carried out. `status` is the HTTP status to answer."""

    status = 400

    def __init__(self, message: str, problems: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.problems = problems or []


class NotFound(SopError):
    status = 404


class WrongState(SopError):
    status = 409


class NotAllowed(SopError):
    status = 403


@dataclass(frozen=True)
class Source:
    """Where an ingested draft came from."""

    source_id: str
    article: str
    revision: str


def who(actor: Actor) -> str:
    return f"{actor.type}:{actor.id}"


def normalise(content: Any) -> Dict[str, Any]:
    """The content as the SOP model reads it, without the fields the lifecycle owns.

    Unknown keys are dropped, so what is hashed and approved is exactly what
    the engine will run.
    """
    if not isinstance(content, dict):
        raise SopError("An SOP is a JSON object")
    try:
        data = SOP.from_dict(content).to_dict()
    except Exception as exc:
        raise SopError(f"The SOP could not be read: {type(exc).__name__}") from None
    data.pop("status", None)
    data.pop("version", None)
    if not _KEY_RE.match(str(data.get("sop_id", ""))):
        raise SopError("sop_id is required: letters, digits, '.', '_' and '-', at most 100")
    return data


def content_hash(content: Dict[str, Any]) -> str:
    return audit.digest(content)


def as_sop(content: Dict[str, Any], version: Any = "", digest: str = "") -> SOP:
    sop = SOP.from_dict({**content, "status": SOP_STATUS_PUBLISHED, "version": str(version or "")})
    sop.content_hash = digest
    return sop


def problems_of(content: Dict[str, Any]) -> List[str]:
    """Why this content could not be published. Empty means it could."""
    try:
        return validate_sop(as_sop(content))
    except Exception as exc:
        return [f"could not be validated: {type(exc).__name__}"]


def capabilities_of(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every connector capability the steps call, with its tier, for the reviewer."""
    used: List[Dict[str, Any]] = []
    registry = default_registry()
    for step in content.get("steps", []):
        step_type = str(step.get("step_type", "")).upper()
        if step_type in CONNECTOR_STEP_TYPES or step_type == StepType.ACTION.value:
            name = step.get("capability") or step_type
        elif step_type == StepType.VERIFY.value:
            name = str((step.get("parameters") or {}).get("check", ""))
        else:
            continue
        capability = find_capability(name)
        owner = registry.owner(capability.id) if capability else None
        manifest = registry.connector_class(owner).manifest if owner else None
        used.append(
            {
                "step_id": step.get("step_id", ""),
                "capability": capability.id if capability else name,
                "known": capability is not None,
                "kind": capability.kind.value if capability else "",
                "tier": capability.tier.value if capability and capability.tier else "",
                "connector": manifest.display_name if manifest else "",
                "preview": bool(manifest and manifest.preview),
            }
        )
    return used


def diff(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> Dict[str, Any]:
    """What changed from the published version, for the reviewer."""
    if old is None:
        return {"first_version": True, "fields": [], "steps": []}
    fields = sorted(
        key for key in (set(old) | set(new)) - {"steps"} if old.get(key) != new.get(key)
    )
    before = {s.get("step_id"): s for s in old.get("steps", [])}
    after = {s.get("step_id"): s for s in new.get("steps", [])}
    steps = [{"step_id": k, "change": "added"} for k in after if k not in before]
    steps += [{"step_id": k, "change": "removed"} for k in before if k not in after]
    steps += [
        {"step_id": k, "change": "changed"} for k in after if k in before and before[k] != after[k]
    ]
    return {"first_version": False, "fields": fields, "steps": steps}


# ── Row helpers ───────────────────────────────────────────────────────


async def _version(conn: AsyncConnection, tenant_id: uuid.UUID, version_id: uuid.UUID, lock=True):
    query = (
        sa.select(sop_versions, sops.c.sop_key)
        .join(sops, sops.c.id == sop_versions.c.sop_id)
        .where(sop_versions.c.tenant_id == tenant_id, sop_versions.c.id == version_id)
    )
    if lock and conn.dialect.name == "postgresql":
        query = query.with_for_update(of=sop_versions)
    row = (await conn.execute(query)).first()
    if row is None:
        raise NotFound("No such SOP version")
    return row


async def _published_content(
    conn: AsyncConnection, tenant_id: uuid.UUID, sop_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return (
        await conn.execute(
            sa.select(sop_versions.c.content).where(
                sop_versions.c.tenant_id == tenant_id,
                sop_versions.c.sop_id == sop_id,
                sop_versions.c.status == SOP_PUBLISHED,
            )
        )
    ).scalar()


def _summary(row) -> Dict[str, Any]:
    return {
        "id": str(row.id),
        "sop_id": row.sop_key,
        "title": row.content.get("title", ""),
        "version": row.version,
        "status": row.status,
        "origin": row.origin,
        "content_hash": row.content_hash,
        "author": row.author_id,
        "approver": row.approver_id or "",
        "review_note": row.review_note,
        "source": {
            "id": row.source_id,
            "article": row.source_article,
            "revision": row.source_revision,
        },
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "published_at": row.published_at.isoformat() if row.published_at else None,
    }


# ── Authoring ─────────────────────────────────────────────────────────


async def create_draft(
    db: Database,
    tenant_id: uuid.UUID,
    actor: Actor,
    content: Any,
    *,
    origin: str = ORIGIN_AUTHORED,
    source: Optional[Source] = None,
    source_text: str = "",
) -> Optional[uuid.UUID]:
    """Add a draft. Returns None when this source revision was already ingested."""
    data = normalise(content)
    now = utcnow()
    version_id = uuid.uuid4()
    try:
        async with db.tenant(tenant_id) as conn:
            sop_id = (
                await conn.execute(
                    sa.select(sops.c.id).where(
                        sops.c.tenant_id == tenant_id, sops.c.sop_key == data["sop_id"]
                    )
                )
            ).scalar()
            if sop_id is None:
                sop_id = uuid.uuid4()
                await conn.execute(
                    sa.insert(sops).values(
                        id=sop_id,
                        tenant_id=tenant_id,
                        sop_key=data["sop_id"],
                        title=str(data.get("title", ""))[:300],
                        created_at=now,
                    )
                )
            if source is not None:
                seen = (
                    await conn.execute(
                        sa.select(sop_versions.c.id).where(
                            sop_versions.c.tenant_id == tenant_id,
                            sop_versions.c.source_id == source.source_id,
                            sop_versions.c.source_article == source.article,
                            sop_versions.c.source_revision == source.revision,
                        )
                    )
                ).first()
                if seen:
                    return None
            await conn.execute(
                sa.insert(sop_versions).values(
                    id=version_id,
                    tenant_id=tenant_id,
                    sop_id=sop_id,
                    status=SOP_DRAFT,
                    content=data,
                    origin=origin,
                    source_id=source.source_id if source else "",
                    source_article=source.article[:500] if source else "",
                    source_revision=source.revision[:200] if source else "",
                    source_text=source_text[:MAX_SOURCE_TEXT],
                    author_id=who(actor),
                    created_at=now,
                    updated_at=now,
                )
            )
            await audit.append(
                conn,
                tenant_id,
                actor,
                "sop.draft_created",
                {
                    "sop_id": data["sop_id"],
                    "version_id": str(version_id),
                    "origin": origin,
                    "content_sha256": content_hash(data),
                    "source": [source.source_id, source.article, source.revision] if source else [],
                },
                correlation_id=f"sop:{data['sop_id']}",
            )
    except IntegrityError:
        # Two ingests of the same revision at once: the unique index let one through.
        if source is not None:
            return None
        raise
    return version_id


async def update_draft(
    db: Database, tenant_id: uuid.UUID, actor: Actor, version_id: uuid.UUID, content: Any
) -> None:
    data = normalise(content)
    async with db.tenant(tenant_id) as conn:
        row = await _version(conn, tenant_id, version_id)
        if row.status != SOP_DRAFT:
            raise WrongState("Only a draft can be edited")
        if data["sop_id"] != row.sop_key:
            raise SopError("sop_id cannot be changed; create a new SOP instead")
        # A draft written by a system (an ingested article) is adopted by its first editor.
        # Anyone else must start their own draft, so that a version has one author.
        if row.author_id != who(actor) and not row.author_id.startswith("system:"):
            raise NotAllowed("Only the author can edit this draft")
        await conn.execute(
            sa.update(sop_versions)
            .where(sop_versions.c.tenant_id == tenant_id, sop_versions.c.id == version_id)
            .values(content=data, author_id=who(actor), updated_at=utcnow())
        )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "sop.draft_updated",
            {
                "sop_id": row.sop_key,
                "version_id": str(version_id),
                "content_sha256": content_hash(data),
                "previous_author": row.author_id,
            },
            correlation_id=f"sop:{row.sop_key}",
        )


async def discard(db: Database, tenant_id: uuid.UUID, actor: Actor, version_id: uuid.UUID) -> None:
    async with db.tenant(tenant_id) as conn:
        row = await _version(conn, tenant_id, version_id)
        if row.status != SOP_DRAFT:
            raise WrongState("Only a draft can be discarded")
        await conn.execute(
            sa.delete(sop_versions).where(
                sop_versions.c.tenant_id == tenant_id, sop_versions.c.id == version_id
            )
        )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "sop.draft_discarded",
            {"sop_id": row.sop_key, "version_id": str(version_id), "author": row.author_id},
            correlation_id=f"sop:{row.sop_key}",
        )


async def submit(db: Database, tenant_id: uuid.UUID, actor: Actor, version_id: uuid.UUID) -> str:
    """Send a draft for review. Returns the hash the reviewer will approve."""
    async with db.tenant(tenant_id) as conn:
        row = await _version(conn, tenant_id, version_id)
        if row.status != SOP_DRAFT:
            raise WrongState("Only a draft can be submitted")
        if row.author_id != who(actor):
            if not row.author_id.startswith("system:"):
                raise NotAllowed("Only the author can submit this draft")
        problems = problems_of(row.content)
        if problems:
            raise SopError("The SOP is not valid and cannot be reviewed", problems)
        digest = content_hash(row.content)
        now = utcnow()
        await conn.execute(
            sa.update(sop_versions)
            .where(sop_versions.c.tenant_id == tenant_id, sop_versions.c.id == version_id)
            .values(
                status=SOP_IN_REVIEW,
                content_hash=digest,
                author_id=who(actor),
                submitted_at=now,
                updated_at=now,
                review_note="",
            )
        )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "sop.submitted",
            {
                "sop_id": row.sop_key,
                "version_id": str(version_id),
                "content_sha256": digest,
                "origin": row.origin,
                "previous_author": row.author_id,
            },
            correlation_id=f"sop:{row.sop_key}",
        )
    return digest


# ── Review ────────────────────────────────────────────────────────────


async def approve(
    db: Database,
    tenant_id: uuid.UUID,
    actor: Actor,
    version_id: uuid.UUID,
    reviewed_hash: str,
) -> None:
    """Approve exactly the content with `reviewed_hash`. The author cannot approve."""
    async with db.tenant(tenant_id) as conn:
        row = await _version(conn, tenant_id, version_id)
        if row.status != SOP_IN_REVIEW:
            raise WrongState("Only a version in review can be approved")
        if row.author_id == who(actor):
            raise NotAllowed("The author of a version cannot approve it")
        if not reviewed_hash or reviewed_hash != row.content_hash:
            raise WrongState("The version changed since it was reviewed; review it again")
        now = utcnow()
        await conn.execute(
            sa.update(sop_versions)
            .where(sop_versions.c.tenant_id == tenant_id, sop_versions.c.id == version_id)
            .values(status=SOP_APPROVED, approver_id=who(actor), approved_at=now, updated_at=now)
        )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "sop.approved",
            {
                "sop_id": row.sop_key,
                "version_id": str(version_id),
                "content_sha256": row.content_hash,
                "author": row.author_id,
                "origin": row.origin,
            },
            correlation_id=f"sop:{row.sop_key}",
        )


async def request_changes(
    db: Database, tenant_id: uuid.UUID, actor: Actor, version_id: uuid.UUID, note: str
) -> None:
    async with db.tenant(tenant_id) as conn:
        row = await _version(conn, tenant_id, version_id)
        if row.status != SOP_IN_REVIEW:
            raise WrongState("Only a version in review can be sent back")
        await conn.execute(
            sa.update(sop_versions)
            .where(sop_versions.c.tenant_id == tenant_id, sop_versions.c.id == version_id)
            .values(status=SOP_DRAFT, review_note=note[:MAX_NOTE], updated_at=utcnow())
        )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "sop.changes_requested",
            {"sop_id": row.sop_key, "version_id": str(version_id), "note": note[:MAX_NOTE]},
            correlation_id=f"sop:{row.sop_key}",
        )


async def publish(db: Database, tenant_id: uuid.UUID, actor: Actor, version_id: uuid.UUID) -> int:
    """Make an approved version the one the engine runs. Returns its version number."""
    async with db.tenant(tenant_id) as conn:
        row = await _version(conn, tenant_id, version_id)
        if row.status != SOP_APPROVED:
            raise WrongState("Only an approved version can be published")
        # A connector may have changed since approval; publish nothing that would not run.
        problems = problems_of(row.content)
        if problems:
            raise SopError("The SOP is no longer valid and cannot be published", problems)
        now = utcnow()
        await conn.execute(
            sa.update(sop_versions)
            .where(
                sop_versions.c.tenant_id == tenant_id,
                sop_versions.c.sop_id == row.sop_id,
                sop_versions.c.status == SOP_PUBLISHED,
            )
            .values(status=SOP_SUPERSEDED, updated_at=now)
        )
        latest = (
            await conn.execute(
                sa.select(sa.func.max(sop_versions.c.version)).where(
                    sop_versions.c.tenant_id == tenant_id, sop_versions.c.sop_id == row.sop_id
                )
            )
        ).scalar()
        number = (latest or 0) + 1
        await conn.execute(
            sa.update(sop_versions)
            .where(sop_versions.c.tenant_id == tenant_id, sop_versions.c.id == version_id)
            .values(status=SOP_PUBLISHED, version=number, published_at=now, updated_at=now)
        )
        await conn.execute(
            sa.update(sops)
            .where(sops.c.tenant_id == tenant_id, sops.c.id == row.sop_id)
            .values(title=str(row.content.get("title", ""))[:300])
        )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "sop.published",
            {
                "sop_id": row.sop_key,
                "version_id": str(version_id),
                "version": number,
                "content_sha256": row.content_hash,
                "author": row.author_id,
                "approver": row.approver_id,
                "origin": row.origin,
            },
            correlation_id=f"sop:{row.sop_key}",
        )
    return number


async def retire(db: Database, tenant_id: uuid.UUID, actor: Actor, sop_key: str) -> None:
    """Withdraw the published version. The SOP is no longer matched."""
    async with db.tenant(tenant_id) as conn:
        row = (
            await conn.execute(
                sa.select(sop_versions.c.id, sop_versions.c.version, sop_versions.c.content_hash)
                .join(sops, sops.c.id == sop_versions.c.sop_id)
                .where(
                    sop_versions.c.tenant_id == tenant_id,
                    sops.c.sop_key == sop_key,
                    sop_versions.c.status == SOP_PUBLISHED,
                )
            )
        ).first()
        if row is None:
            raise NotFound("This SOP has no published version")
        now = utcnow()
        await conn.execute(
            sa.update(sop_versions)
            .where(sop_versions.c.tenant_id == tenant_id, sop_versions.c.id == row.id)
            .values(status=SOP_RETIRED, retired_at=now, updated_at=now)
        )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "sop.retired",
            {"sop_id": sop_key, "version": row.version, "content_sha256": row.content_hash},
            correlation_id=f"sop:{sop_key}",
        )


# ── Reads ─────────────────────────────────────────────────────────────


async def published(db: Database, tenant_id: uuid.UUID) -> List[SOP]:
    """What the engine may match. Read through the view, never from the table."""
    async with db.tenant(tenant_id) as conn:
        rows = await conn.execute(
            sa.select(published_sops)
            .where(published_sops.c.tenant_id == tenant_id)
            .order_by(published_sops.c.sop_key)
        )
        return [as_sop(r.content, r.version, r.content_hash) for r in rows]


async def version_for_resume(
    db: Database, tenant_id: uuid.UUID, sop_key: str, version: str, digest: str
) -> Optional[SOP]:
    """The exact version an interrupted run started with, unless it was withdrawn.

    A version replaced by a newer one may still finish the run it started. A
    retired version may not.
    """
    if not digest or not str(version).isdigit():
        return None
    async with db.tenant(tenant_id) as conn:
        row = (
            await conn.execute(
                sa.select(sop_versions.c.content, sop_versions.c.version)
                .join(sops, sops.c.id == sop_versions.c.sop_id)
                .where(
                    sop_versions.c.tenant_id == tenant_id,
                    sops.c.sop_key == sop_key,
                    sop_versions.c.version == int(version),
                    sop_versions.c.content_hash == digest,
                    sop_versions.c.status.in_((SOP_PUBLISHED, SOP_SUPERSEDED)),
                )
            )
        ).first()
    return as_sop(row.content, row.version, digest) if row else None


async def list_versions(
    db: Database, tenant_id: uuid.UUID, *, sop_key: str = "", status: str = ""
) -> List[Dict[str, Any]]:
    query = (
        sa.select(sop_versions, sops.c.sop_key)
        .join(sops, sops.c.id == sop_versions.c.sop_id)
        .where(sop_versions.c.tenant_id == tenant_id)
        .order_by(sops.c.sop_key, sop_versions.c.created_at.desc())
        .limit(1000)
    )
    if sop_key:
        query = query.where(sops.c.sop_key == sop_key)
    if status:
        query = query.where(sop_versions.c.status == status)
    async with db.tenant(tenant_id) as conn:
        return [_summary(r) for r in await conn.execute(query)]


async def review(db: Database, tenant_id: uuid.UUID, version_id: uuid.UUID) -> Dict[str, Any]:
    """Everything a reviewer sees: content, source, checks, capabilities and what changed."""
    async with db.tenant(tenant_id) as conn:
        row = await _version(conn, tenant_id, version_id, lock=False)
        current = None
        if row.status != SOP_PUBLISHED:
            current = await _published_content(conn, tenant_id, row.sop_id)
    return {
        **_summary(row),
        "content": row.content,
        "source_text": row.source_text,
        # Recomputed on every read: a draft's hash is not stored until it is submitted.
        "current_hash": content_hash(row.content),
        "problems": problems_of(row.content),
        "capabilities": capabilities_of(row.content),
        "diff": diff(current, row.content),
    }


# ── Demo data ─────────────────────────────────────────────────────────

SAMPLE_AUTHOR = Actor.system("sample-library")
SAMPLE_APPROVER = Actor.system("sample-reviewer")


async def seed_published(db: Database, tenant_id: uuid.UUID, contents: List[dict]) -> int:
    """Publish the sample SOPs through the real lifecycle. Demo mode only.

    An SOP that already has any version is left alone.
    """
    existing = {v["sop_id"] for v in await list_versions(db, tenant_id)}
    done = 0
    for content in contents:
        data = normalise(content)
        if data["sop_id"] in existing:
            continue
        version_id = await create_draft(db, tenant_id, SAMPLE_AUTHOR, data, origin=ORIGIN_SEED)
        digest = await submit(db, tenant_id, SAMPLE_AUTHOR, version_id)
        await approve(db, tenant_id, SAMPLE_APPROVER, version_id, digest)
        await publish(db, tenant_id, SAMPLE_APPROVER, version_id)
        done += 1
    return done
