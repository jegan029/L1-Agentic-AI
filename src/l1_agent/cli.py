"""Command line: `l1-agent <command>`.

    serve                       HTTP listener, poller and embedded workers
    worker                      workers only
    migrate                     apply schema migrations (migration role)
    tenant create --slug S      create a tenant (migration role)
    roles grant|remove          map an identity provider group to a role
    token create|revoke|list    scoped API tokens
    audit verify|checkpoint|timeline
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

from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.auth import breakglass, store
from src.l1_agent.config.settings import ConfigError, Settings
from src.l1_agent.db.engine import Database
from src.l1_agent.db.migrate import upgrade

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


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
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
        return asyncio.run(_run(args, settings))
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
