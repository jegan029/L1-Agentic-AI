"""Command line: `l1-agent <command>`.

    serve                       HTTP listener, poller and embedded workers
    worker                      workers only
    migrate                     apply schema migrations (migration role)
    tenant create --slug S      create a tenant (migration role)
    roles grant|remove          map an identity provider group to a role
    token create|revoke|list    scoped API tokens
    audit verify|checkpoint|timeline
    sop import --file F         add an SOP as a draft (it still needs review and approval)
    sop list
    connection set|remove|list|test   a connector's settings and credential references
    connectors                  the connectors in this build and what each can do
    knowledge sync              pull changed articles from the SOP sources as drafts
    breakglass hash             hash a break-glass password

Commands that change access write to the audit log as actor `system:cli`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from src.l1_agent.api.security import SecurityConfigError
from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.auth import breakglass, store
from src.l1_agent.config.settings import ConfigError, Settings
from src.l1_agent.connectors import hub as connections
from src.l1_agent.connectors.catalog import default_registry
from src.l1_agent.db.engine import Database
from src.l1_agent.db.migrate import upgrade
from src.l1_agent.sop import ingest
from src.l1_agent.sop import store as sop_store

CLI = Actor.system("cli")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="l1-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("worker")
    sub.add_parser("migrate")

    tenant = sub.add_parser("tenant").add_subparsers(dest="action", required=True)
    create = tenant.add_parser("create")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", default="")

    roles = sub.add_parser("roles").add_subparsers(dest="action", required=True)
    for action in ("grant", "remove"):
        p = roles.add_parser(action)
        p.add_argument("--group", required=True)
        p.add_argument("--role", required=True)
    roles.add_parser("list")

    token = sub.add_parser("token").add_subparsers(dest="action", required=True)
    make = token.add_parser("create")
    make.add_argument("--name", required=True)
    make.add_argument("--permission", action="append", required=True, dest="permissions")
    make.add_argument("--days", type=int, default=90)
    revoke = token.add_parser("revoke")
    revoke.add_argument("--id", required=True)
    token.add_parser("list")

    aud = sub.add_parser("audit").add_subparsers(dest="action", required=True)
    verify = aud.add_parser("verify")
    verify.add_argument("--checkpoints", help="file of checkpoints, one JSON object per line")
    aud.add_parser("checkpoint")
    timeline = aud.add_parser("timeline")
    timeline.add_argument("--run", required=True)

    sop = sub.add_parser("sop").add_subparsers(dest="action", required=True)
    imported = sop.add_parser("import")
    imported.add_argument("--file", required=True)
    sop.add_parser("list")

    connection = sub.add_parser("connection").add_subparsers(dest="action", required=True)
    setter = connection.add_parser("set")
    setter.add_argument("--connector", required=True)
    setter.add_argument("--config", help="JSON file of the connector's settings")
    setter.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="SLOT=REFERENCE",
        help="where a credential is kept, e.g. token=vault:splunk/token",
    )
    setter.add_argument("--disable", action="store_true")
    for action in ("remove", "test"):
        connection.add_parser(action).add_argument("--connector", required=True)
    connection.add_parser("list")
    sub.add_parser("connectors")
    sub.add_parser("knowledge").add_subparsers(dest="action", required=True).add_parser("sync")

    bg = sub.add_parser("breakglass").add_subparsers(dest="action", required=True)
    bg.add_parser("hash")
    return parser


async def _tenant(db: Database, settings: Settings) -> uuid.UUID:
    tenant_id = await db.tenant_id_for(settings.database.tenant_slug)
    if tenant_id is None:
        raise ConfigError(f"Tenant '{settings.database.tenant_slug}' does not exist")
    return tenant_id


def _migration_url(settings: Settings) -> str:
    if not settings.database.migration_url:
        raise ConfigError("DATABASE_MIGRATION_URL is required for this command")
    return settings.database.migration_url


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "tenant":
        owner = Database.create(_migration_url(settings), demo_mode=False)
        try:
            tenant_id = await owner.ensure_tenant(args.slug, args.name)
            async with owner.tenant(tenant_id) as conn:
                await audit.append(conn, tenant_id, CLI, "tenant.created", {"slug": args.slug})
        finally:
            await owner.close()
        print(f"tenant {args.slug} {tenant_id}")
        return 0

    db = Database.create(settings.database.url, demo_mode=settings.agent.demo_mode)
    try:
        tenant_id = await _tenant(db, settings)
        if args.command == "roles":
            if args.action == "list":
                for m in await store.list_role_mappings(db, tenant_id):
                    print(f"{m['group']}\t{m['role']}")
                return 0
            changed = await store.set_role_mapping(
                db, tenant_id, args.group, args.role, CLI, grant=args.action == "grant"
            )
            print("changed" if changed else "unchanged")
            return 0

        if args.command == "token":
            if args.action == "list":
                for t in await store.list_api_tokens(db, tenant_id):
                    state = "revoked" if t["revoked"] else "active"
                    print(f"{t['id']}\t{t['name']}\t{state}\t{','.join(t['permissions'])}")
                return 0
            if args.action == "revoke":
                done = await store.revoke_api_token(db, tenant_id, uuid.UUID(args.id), CLI)
                print("revoked" if done else "not found")
                return 0 if done else 1
            token, token_id = await store.create_api_token(
                db, tenant_id, args.name, args.permissions, args.days, CLI
            )
            print(f"id {token_id}")
            print(f"token {token}")
            print("Store the token now; it cannot be shown again.", file=sys.stderr)
            return 0

        if args.command == "sop":
            if args.action == "list":
                for v in await sop_store.list_versions(db, tenant_id):
                    number = v["version"] if v["version"] is not None else "-"
                    print(f"{v['sop_id']}\t{number}\t{v['status']}\t{v['id']}\t{v['title']}")
                return 0
            content = json.loads(Path(args.file).read_text(encoding="utf-8"))
            try:
                version_id = await sop_store.create_draft(db, tenant_id, CLI, content)
            except sop_store.SopError as exc:
                raise ConfigError(str(exc)) from None
            print(f"draft {version_id}")
            print("A person must submit it and a different person must approve it.")
            return 0

        if args.command == "connection" and args.action in ("set", "remove", "list"):
            if args.action == "list":
                stored = await connections.stored_connections(db, tenant_id)
                for connector_id, c in sorted(stored.items()):
                    state = "enabled" if c.enabled else "disabled"
                    refs = ",".join(f"{k}={v}" for k, v in sorted(c.secret_refs.items()))
                    print(f"{connector_id}\t{state}\t{refs}")
                return 0
            if args.action == "remove":
                done = await connections.remove_connection(db, tenant_id, CLI, args.connector)
                print("removed" if done else "not found")
                return 0 if done else 1
            config = {}
            if args.config:
                config = json.loads(Path(args.config).read_text(encoding="utf-8"))
            refs = {}
            for item in args.secret:
                slot, sep, reference = item.partition("=")
                if not sep or not slot or not reference:
                    raise ConfigError("--secret takes SLOT=REFERENCE")
                refs[slot] = reference
            await connections.set_connection(
                db, tenant_id, CLI, args.connector, config, refs, enabled=not args.disable
            )
            print("saved")
            return 0

        if args.command == "audit":
            key = settings.auth.audit_checkpoint_key
            if args.action == "checkpoint":
                if not key:
                    raise ConfigError("AUDIT_CHECKPOINT_KEY is required to sign a checkpoint")
                print(json.dumps(await audit.checkpoint(db, tenant_id, key)))
                return 0
            if args.action == "timeline":
                for event in await audit.events_for(
                    db, tenant_id, correlation_id=args.run, limit=10_000
                ):
                    print(json.dumps(event))
                return 0
            checkpoints = []
            if args.checkpoints:
                if not key:
                    raise ConfigError("AUDIT_CHECKPOINT_KEY is required to check checkpoints")
                lines = Path(args.checkpoints).read_text(encoding="utf-8").splitlines()
                checkpoints = [json.loads(line) for line in lines if line.strip()]
            result = await audit.verify(db, tenant_id, checkpoints=checkpoints, checkpoint_key=key)
            print(
                f"events checked: {result.events_checked}, head: {result.head_seq} "
                f"{result.head_hash}, checkpoints compared: {len(checkpoints)}"
            )
            for problem in result.problems:
                print(f"PROBLEM: {problem}")
            print("OK" if result.ok else "FAILED")
            return 0 if result.ok else 2
    finally:
        await db.close()
    return 1


async def _with_service(args: argparse.Namespace, settings: Settings) -> int:
    """Commands that need the connectors themselves, not only the database."""
    from src.l1_agent.main import L1AgentService

    service = L1AgentService(settings)
    try:
        ctx = await service.setup()
        if args.command == "knowledge":
            sources = await ctx.hub.knowledge_sources(ctx.tenant_id, ctx.itsm)
            if not sources:
                print("no knowledge source is configured")
                return 0
            failed = False
            for result in await ingest.sync_all(ctx.db, ctx.tenant_id, sources, CLI):
                print(json.dumps(result.to_dict()))
                failed = failed or bool(result.errors)
            return 2 if failed else 0
        try:
            checks = await ctx.hub.test(ctx.tenant_id, args.connector)
        except LookupError:
            raise ConfigError(f"Unknown connector '{args.connector}'") from None
        for check in checks:
            print(f"{'ok  ' if check.ok else 'FAIL'} {check.name} {check.detail}".rstrip())
        return 0 if all(c.ok for c in checks) else 2
    finally:
        await service.close()


def _print_connectors() -> None:
    for manifest in default_registry().manifests():
        flag = " (preview)" if manifest.preview else ""
        print(f"{manifest.id}\t{manifest.family.value}\t{manifest.display_name}{flag}")
        for slot in manifest.secrets:
            print(f"    credential {slot.name}: {slot.purpose}")
        for cap in manifest.capabilities:
            tier = cap.tier.value if cap.tier else "-"
            print(f"    {cap.id}\t{cap.kind.value}\t{tier}\t{cap.summary}")


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "connectors":
        _print_connectors()
        return 0
    if args.command == "breakglass":
        first = getpass.getpass("Break-glass password: ")
        if first != getpass.getpass("Again: "):
            print("The passwords do not match", file=sys.stderr)
            return 1
        print(breakglass.hash_password(first))
        return 0

    from src.l1_agent.main import L1AgentService, load_settings

    try:
        settings = load_settings()
        if args.command == "serve":
            asyncio.run(L1AgentService(settings).start())
            return 0
        if args.command == "worker":
            asyncio.run(L1AgentService(settings).run_worker_only())
            return 0
        if args.command == "migrate":
            upgrade(_migration_url(settings), settings.database.app_role)
            print("migrations applied")
            return 0
        if args.command == "knowledge" or (args.command == "connection" and args.action == "test"):
            return asyncio.run(_with_service(args, settings))
        return asyncio.run(_run(args, settings))
    except (ConfigError, SecurityConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
