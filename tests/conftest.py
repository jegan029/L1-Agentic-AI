"""Database fixtures.

Tests that take `db` run twice: on SQLite (what demo mode uses) and on a real
PostgreSQL. Tests that take `pg` need PostgreSQL features (row level security,
row locks, triggers) and run only there.

PostgreSQL tests need L1_TEST_PG_ADMIN_URL, a superuser connection used to
create a scratch database, for example
    postgresql://postgres:postgres@127.0.0.1:5432/postgres
Without it they are skipped, unless L1_REQUIRE_POSTGRES=1 (set in CI), in
which case they fail, so a misconfigured pipeline cannot skip them silently.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import pytest

from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.config.settings import Settings
from src.l1_agent.connectors.catalog import connectors_for_adapters
from src.l1_agent.db.engine import Database
from src.l1_agent.db.migrate import upgrade
from src.l1_agent.demo import MockServiceNowClient, build_mock_adapters, load_sample_sops
from src.l1_agent.engine.executor import SOPExecutor, _FixedConnectors
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.service.runs import DbEventBus, RunService

ADMIN_URL = os.environ.get("L1_TEST_PG_ADMIN_URL", "")
APP_LOGIN = "l1_test_app"
APP_PASSWORD = "l1_test_app_pw"


def _with(url: str, *, scheme: str, database: str, userinfo: Optional[str] = None) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    netloc = f"{userinfo}@{host}" if userinfo else parts.netloc
    return urlunsplit((scheme, netloc, "/" + database, "", ""))


def _off_loop(fn) -> None:
    """Run blocking set-up in its own thread; a test's event loop may be running here."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(fn).result()


@dataclass(frozen=True)
class PgUrls:
    app: str  # the application role: row level security applies
    owner: str  # the migration role
    admin_dsn: str  # plain asyncpg DSN for the scratch database, as superuser


@pytest.fixture(scope="session")
def pg_urls() -> PgUrls:
    if not ADMIN_URL:
        if os.environ.get("L1_REQUIRE_POSTGRES") == "1":
            pytest.fail("L1_REQUIRE_POSTGRES=1 but L1_TEST_PG_ADMIN_URL is not set")
        pytest.skip("PostgreSQL tests need L1_TEST_PG_ADMIN_URL")
    import asyncpg

    name = f"l1_test_{uuid.uuid4().hex[:10]}"

    async def create() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        try:
            await conn.execute(f"CREATE DATABASE {name}")
            exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", APP_LOGIN)
            if not exists:
                await conn.execute(f"CREATE ROLE {APP_LOGIN} LOGIN PASSWORD '{APP_PASSWORD}'")
        finally:
            await conn.close()

    async def grant() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        try:
            await conn.execute(f"GRANT l1_app TO {APP_LOGIN}")
        finally:
            await conn.close()

    async def drop() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        try:
            await conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        finally:
            await conn.close()

    _off_loop(lambda: asyncio.run(create()))
    owner = _with(ADMIN_URL, scheme="postgresql+asyncpg", database=name)
    _off_loop(lambda: upgrade(owner))
    _off_loop(lambda: asyncio.run(grant()))
    yield PgUrls(
        app=_with(
            ADMIN_URL,
            scheme="postgresql+asyncpg",
            database=name,
            userinfo=f"{APP_LOGIN}:{APP_PASSWORD}",
        ),
        owner=owner,
        admin_dsn=_with(ADMIN_URL, scheme="postgresql", database=name),
    )
    _off_loop(lambda: asyncio.run(drop()))


@dataclass
class Backend:
    """A database plus a way to create tenants in it."""

    db: Database
    name: str
    _owner: Database
    admin_dsn: str = ""

    async def new_tenant(self, label: str = "t") -> uuid.UUID:
        return await self._owner.ensure_tenant(f"{label}-{uuid.uuid4().hex[:10]}")

    async def tamper(self, sql: str, *args) -> None:
        """Change audit rows the way someone with full database access could.

        On PostgreSQL that takes a superuser and switching the guard triggers off.
        """
        if self.name == "sqlite":
            async with self.db.platform() as conn:
                await conn.exec_driver_sql(sql.replace("$1", "?").replace("$2", "?"), args)
            return
        import asyncpg

        conn = await asyncpg.connect(self.admin_dsn)
        try:
            await conn.execute("ALTER TABLE audit_events DISABLE TRIGGER USER")
            await conn.execute(sql, *args)
        finally:
            await conn.execute("ALTER TABLE audit_events ENABLE TRIGGER USER")
            await conn.close()


async def _sqlite_backend(tmp_path) -> Backend:
    db = Database.create(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", demo_mode=True)
    await db.create_schema_for_demo()
    return Backend(db, "sqlite", db)


@pytest.fixture
async def pg(pg_urls: PgUrls) -> Backend:
    db = Database.create(pg_urls.app, demo_mode=False)
    owner = Database.create(pg_urls.owner, demo_mode=False)
    yield Backend(db, "postgres", owner, pg_urls.admin_dsn)
    await db.close()
    await owner.close()


@pytest.fixture(params=["sqlite", "postgres"])
async def backend(request, tmp_path) -> Backend:
    if request.param == "sqlite":
        made = await _sqlite_backend(tmp_path)
        yield made
        await made.db.close()
        return
    urls: PgUrls = request.getfixturevalue("pg_urls")
    db = Database.create(urls.app, demo_mode=False)
    owner = Database.create(urls.owner, demo_mode=False)
    yield Backend(db, "postgres", owner, urls.admin_dsn)
    await db.close()
    await owner.close()


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    monkeypatch.setattr(
        "src.l1_agent.escalation.escalator.send_escalation_email", lambda *a, **k: False
    )


def make_service(
    db: Database,
    *,
    adapters: Optional[Dict[str, BaseAdapter]] = None,
    snow: Optional[MockServiceNowClient] = None,
    lease_seconds: int = 300,
    max_attempts: int = 3,
    worker_id: str = "",
    wrap: Optional[Callable[[IncidentProcessor], IncidentProcessor]] = None,
    sops_from_store: bool = False,
    actions=None,
    connector_settings: Optional[Dict[str, dict]] = None,
    advisors=None,
    connectors=None,
) -> RunService:
    """A run service wired like the real one, with mock adapters and mock ServiceNow.

    `actions` is the action pipeline (an ActionService); without one, ACTION steps are
    refused. `connector_settings` gives a mock connector real connection settings, for
    example an allow list. `advisors` is the model gateway; without one no model is used.
    `connectors` replaces the mocks with something that has `for_tenant`: a hub whose
    calls go to a runner.
    """
    bus = DbEventBus(db)
    fixed = connectors or _FixedConnectors(
        connectors_for_adapters(adapters or build_mock_adapters(), connector_settings)
    )
    processor = IncidentProcessor(
        snow_client=snow or MockServiceNowClient(),  # type: ignore[arg-type]
        sop_matcher=SOPMatcher(confidence_threshold=0.6),
        sop_parser=SOPParser(),
        executor=SOPExecutor(
            connectors=fixed,
            retry_max_attempts=1,
            retry_base_delay=0,
            event_bus=bus,  # type: ignore[arg-type]
            actions=actions,
        ),
        # With the version store, the processor has no SOPs of its own.
        sop_cache=None if sops_from_store else load_sample_sops(),
        confidence_threshold=0.6,
        event_bus=bus,  # type: ignore[arg-type]
        l2_router=L2Router(),
        advisors=advisors,
    )
    if wrap:
        processor = wrap(processor)
    return RunService(
        db,
        processor,
        worker_id=worker_id,
        visibility_timeout_seconds=lease_seconds,
        max_attempts=max_attempts,
        sops_from_store=sops_from_store,
        actions=actions,
    )


@pytest.fixture
def demo_settings() -> Settings:
    """Settings configured for demo/test mode."""
    return Settings.from_env()


# ── A developer's .env is not part of the tests ───────────────────────

LIVE_MARKERS = ("live_servicenow", "live_llm", "splunk_live")
_LOCAL_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


@pytest.fixture(autouse=True)
def _no_local_env_file(request, monkeypatch):
    """Keep the settings in a developer's `.env` out of every test that is not a live test.

    `.env` says how one machine runs the service (demo mode, which connectors are
    real). `Settings.from_env` reads it, and the live test modules load it when
    they are collected, so without this a test's result depends on that file. CI
    has no `.env`; this makes a developer's run the same.
    """
    if any(request.node.get_closest_marker(marker) for marker in LIVE_MARKERS):
        return
    from dotenv import dotenv_values

    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    if os.path.isfile(_LOCAL_ENV_FILE):
        for name in dotenv_values(_LOCAL_ENV_FILE):
            monkeypatch.delenv(name, raising=False)
