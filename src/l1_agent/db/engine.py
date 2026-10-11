"""Database access: one engine, and connections that are bound to one tenant.

All tenant data is read and written through `Database.tenant(tenant_id)`.
On PostgreSQL that sets `app.tenant_id` for the transaction, which the row
level security policies read. Code that forgets its own tenant filter still
cannot see another tenant's rows.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from src.l1_agent.config.settings import ConfigError
from src.l1_agent.db.models import metadata, tenants

DEMO_TENANT_SLUG = "demo"


def _fresh_demo_url() -> str:
    """A new SQLite file in a temporary directory, removed when the process exits."""
    directory = tempfile.mkdtemp(prefix="l1-agent-demo-")
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    return "sqlite+aiosqlite:///" + (Path(directory) / "demo.db").as_posix()


def _configure_sqlite(engine: AsyncEngine) -> None:
    """Make SQLite transactions take the write lock at BEGIN.

    Each transaction then sees a consistent head of the audit chain and of the
    job queue, which PostgreSQL provides through locks SQLite does not have.
    """

    @sa.event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @sa.event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UnsafeDatabaseRole(ConfigError):
    """The application is connected as a role that can bypass tenant isolation."""


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.is_postgres = engine.dialect.name == "postgresql"

    @classmethod
    def create(cls, url: str, *, demo_mode: bool) -> "Database":
        """Build the engine. SQLite is accepted only in demo mode (ADR 0004)."""
        if not url and demo_mode:
            url = _fresh_demo_url()
        if not url:
            raise ConfigError("DATABASE_URL is required outside demo mode")
        if url.startswith("sqlite"):
            if not demo_mode:
                raise ConfigError("SQLite is accepted only with AGENT_DEMO_MODE=true")
            if ":memory:" in url or url.rstrip("/") == "sqlite+aiosqlite:":
                raise ConfigError("Demo mode needs a file-backed SQLite database")
            engine = create_async_engine(url)
            _configure_sqlite(engine)
            return cls(engine)
        if not url.startswith("postgresql+asyncpg://"):
            raise ConfigError("DATABASE_URL must start with postgresql+asyncpg://")
        return cls(create_async_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=5))

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def tenant(self, tenant_id: uuid.UUID) -> AsyncIterator[AsyncConnection]:
        """A transaction that can only see `tenant_id`'s rows. Commits on exit."""
        if not isinstance(tenant_id, uuid.UUID):
            raise TypeError("tenant_id must be a UUID")
        async with self.engine.begin() as conn:
            if self.is_postgres:
                await conn.execute(
                    sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
                )
            yield conn

    @asynccontextmanager
    async def platform(self) -> AsyncIterator[AsyncConnection]:
        """A transaction with no tenant set. It can read `tenants` and nothing else."""
        async with self.engine.begin() as conn:
            yield conn

    # ── Set-up ────────────────────────────────────────────────────────

    async def create_schema_for_demo(self) -> None:
        """Create tables directly. PostgreSQL schemas come from migrations only."""
        if self.is_postgres:
            raise ConfigError("Use `l1-agent migrate` to create a PostgreSQL schema")
        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def ensure_tenant(self, slug: str, name: str = "") -> uuid.UUID:
        """Return the tenant's id, creating the row if this role is allowed to."""
        async with self.platform() as conn:
            found = (
                await conn.execute(sa.select(tenants.c.id).where(tenants.c.slug == slug))
            ).scalar()
            if found:
                return found
            tenant_id = uuid.uuid4()
            await conn.execute(
                sa.insert(tenants).values(
                    id=tenant_id, slug=slug, name=name or slug, status="active", created_at=utcnow()
                )
            )
            return tenant_id

    async def tenant_id_for(self, slug: str) -> Optional[uuid.UUID]:
        async with self.platform() as conn:
            return (
                await conn.execute(
                    sa.select(tenants.c.id).where(
                        tenants.c.slug == slug, tenants.c.status == "active"
                    )
                )
            ).scalar()

    async def active_tenants(self) -> list:
        async with self.platform() as conn:
            rows = await conn.execute(sa.select(tenants.c.id).where(tenants.c.status == "active"))
            return [r[0] for r in rows]

    async def check_role_is_safe(self) -> None:
        """Refuse a connection that row level security does not apply to.

        Superusers and roles with BYPASSRLS ignore every policy. A role that can
        create tenants or change audit rows is the migration role, not the
        application role.
        """
        if not self.is_postgres:
            return
        async with self.platform() as conn:
            row = (
                await conn.execute(
                    sa.text(
                        "SELECT rolsuper, rolbypassrls, "
                        "has_table_privilege(current_user, 'audit_events', 'UPDATE'), "
                        "has_table_privilege(current_user, 'audit_events', 'DELETE'), "
                        "has_table_privilege(current_user, 'tenants', 'INSERT'), "
                        "(SELECT bool_or(pg_has_role(current_user, c.relowner, 'MEMBER')) "
                        "   FROM pg_class c WHERE c.relname IN ('runs', 'audit_events') "
                        "    AND c.relnamespace = current_schema()::regnamespace) "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
        labels = (
            "is a superuser",
            "has BYPASSRLS",
            "can UPDATE audit_events",
            "can DELETE audit_events",
            "can INSERT tenants",
            "owns the tables",
        )
        problems = [text for bad, text in zip(row, labels) if bad]
        if problems:
            raise UnsafeDatabaseRole(
                "Refusing to start: the database role " + ", ".join(problems) + ". Connect as "
                "the application role created by the migration, not the migration role."
            )
