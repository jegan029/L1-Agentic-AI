"""A tenant's data, written out in plain files, for leaving or for keeping a copy.

    manifest.json            what is here, when it was written, and a SHA-256 of each file
    <table>.jsonl            one JSON object per line: the rows of one table, as stored

Written: SOPs and every version of them, connections (settings and credential
references), the action policy, the model policy, role mappings, incidents, runs and
their steps, actions, the record of model calls, enrolled runners, knowledge source
cursors, and the audit log exactly as it is stored, so that its hash chain can be
checked from the files alone (`verify_export`).

Not written, on purpose: users and sessions (personal data that belongs to the
identity provider), API token hashes and anything else that authenticates, and the
queues, which are work in progress and not a record. No credential is written: the
product never holds one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import sqlalchemy as sa

from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.db.models import (
    actions,
    audit_events,
    connections,
    incidents,
    knowledge_sources,
    model_calls,
    model_policies,
    policies,
    role_mappings,
    run_steps,
    runners,
    runs,
    sop_versions,
    sops,
)

FORMAT = 1
MANIFEST = "manifest.json"
# What is written, and the column each file is ordered by.
TABLES = (
    (sops, "created_at"),
    (sop_versions, "created_at"),
    (connections, "connector_id"),
    (policies, "tenant_id"),
    (model_policies, "tenant_id"),
    (role_mappings, "idp_group"),
    (knowledge_sources, "source_id"),
    (runners, "created_at"),
    (incidents, "received_at"),
    (runs, "created_at"),
    (run_steps, "started_at"),
    (actions, "created_at"),
    (model_calls, "created_at"),
    (audit_events, "seq"),
)
NOT_EXPORTED = (
    "users",
    "sessions",
    "login_states",
    "api_tokens",
    "webhook_nonces",
    "jobs",
    "events",
    "runner_calls",
)


class ExportError(ValueError):
    """The export could not be written, or one that was read is not intact."""


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def export_tenant(
    db: Database, tenant_id: uuid.UUID, slug: str, directory: Path, actor: Actor
) -> Dict[str, Any]:
    """Write the tenant's data to an empty directory and record that it was done."""
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ExportError(f"{directory} is not empty; an export is never written over another")
    directory.mkdir(parents=True, exist_ok=True)

    files: Dict[str, Dict[str, Any]] = {}
    async with db.tenant(tenant_id) as conn:
        for table, order in TABLES:
            name = f"{table.name}.jsonl"
            digest, count = hashlib.sha256(), 0
            rows = await conn.stream(
                sa.select(table).where(table.c.tenant_id == tenant_id).order_by(table.c[order])
            )
            with (directory / name).open("w", encoding="utf-8", newline="\n") as out:
                async for row in rows:
                    line = json.dumps(
                        {k: _plain(v) for k, v in row._mapping.items()},
                        sort_keys=True,
                        ensure_ascii=True,
                        default=str,
                    )
                    out.write(line + "\n")
                    digest.update(line.encode("ascii") + b"\n")
                    count += 1
            files[name] = {"rows": count, "sha256": digest.hexdigest()}

    manifest = {
        "format": FORMAT,
        "tenant_id": str(tenant_id),
        "tenant_slug": slug,
        "exported_at": utcnow().isoformat(),
        "files": files,
        "not_exported": list(NOT_EXPORTED),
    }
    (directory / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Recorded after the files are written, so the export does not contain its own event.
    async with db.tenant(tenant_id) as conn:
        await audit.append(
            conn,
            tenant_id,
            actor,
            "tenant.exported",
            {"files": {name: info["rows"] for name, info in files.items()}, "format": FORMAT},
        )
    return manifest


def verify_export(directory: Path) -> List[str]:
    """Problems with an export, read from its files alone. Empty means it is intact.

    Each file matches the digest in the manifest, and the audit log's chain holds
    from its first event to its last: nothing changed, removed or reordered.
    """
    directory = Path(directory)
    try:
        manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
        tenant_id = uuid.UUID(manifest["tenant_id"])
        listed = dict(manifest["files"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"the manifest cannot be read: {type(exc).__name__}"]

    problems: List[str] = []
    for name, info in sorted(listed.items()):
        path = directory / name
        if not path.is_file():
            problems.append(f"{name} is missing")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != info.get("sha256"):
            problems.append(f"{name} does not match the manifest")

    log = directory / f"{audit_events.name}.jsonl"
    if not log.is_file():
        return problems
    expected_seq, prev_hash = 1, audit.GENESIS_HASH
    with log.open(encoding="utf-8") as lines:
        for line in lines:
            try:
                row = json.loads(line)
                recomputed = audit.event_hash(
                    tenant_id,
                    row["seq"],
                    row["occurred_at"],
                    row["actor_type"],
                    row["actor_id"],
                    row["correlation_id"],
                    row["event_type"],
                    row["payload"],
                    row["prev_hash"],
                )
            except (ValueError, KeyError, TypeError):
                problems.append(f"audit event {expected_seq}: the line cannot be read")
                break
            if row["seq"] != expected_seq:
                problems.append(f"audit log: expected event {expected_seq}, found {row['seq']}")
            if row["prev_hash"] != prev_hash:
                problems.append(f"audit event {row['seq']}: does not follow the event before it")
            if recomputed != row["hash"]:
                problems.append(f"audit event {row['seq']}: content does not match its hash")
            prev_hash, expected_seq = row["hash"], row["seq"] + 1
    return problems
