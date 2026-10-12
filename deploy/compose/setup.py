"""One-time set-up for the compose demo. DEMO ONLY.

Does what an operator does by hand for a real deployment (docs/operator-guide.md):
creates the two database roles, applies the migrations, creates the tenant, makes the
three key pairs and enrols the runner's public key. Every step is safe to run again.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

from src.l1_agent import cli

KEYS = Path("/keys")
APP_UID = 10001


async def roles() -> None:
    """The group role the migrations grant to, and the login the application uses."""
    dsn = os.environ["DATABASE_MIGRATION_URL"].replace("postgresql+asyncpg://", "postgresql://")
    login, password = os.environ["APP_LOGIN"], os.environ["APP_PASSWORD"]
    conn = await asyncpg.connect(dsn)
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'l1_app'"):
            await conn.execute("CREATE ROLE l1_app NOLOGIN")
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", login):
            # Identifiers and the password come from this demo's own compose file.
            await conn.execute(f"CREATE ROLE {login} LOGIN PASSWORD '{password}' IN ROLE l1_app")
    finally:
        await conn.close()


def step(*argv: str) -> None:
    print("+ l1-agent", *argv, flush=True)
    code = cli.main(list(argv))
    if code != 0:
        sys.exit(code)


def main() -> None:
    asyncio.run(roles())
    step("migrate")
    step("tenant", "create", "--slug", os.environ["TENANT_SLUG"], "--name", "Demo tenant")
    for name in ("dispatch", "grant", "runner"):
        if not (KEYS / f"{name}.key").exists():
            step("runner", "keygen", "--out", str(KEYS / f"{name}.key"))
    for path in KEYS.iterdir():
        os.chown(path, APP_UID, APP_UID)
        path.chmod(0o440)
    step(
        "runner",
        "enrol",
        "--name",
        "demo-runner",
        "--public-key-file",
        str(KEYS / "runner.key.pub"),
        "--if-absent",
    )


if __name__ == "__main__":
    main()
