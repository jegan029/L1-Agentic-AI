"""Tenant isolation is enforced by the database, not by remembering a filter.

Phase 2 exit criterion 2. Every query below deliberately leaves out the
tenant filter; row level security is the only thing separating the tenants.
PostgreSQL only: SQLite (demo mode) has one tenant and no row level security.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from src.l1_agent.audit.log import Actor, append
from src.l1_agent.connectors import hub as connections
from src.l1_agent.db.engine import Database, UnsafeDatabaseRole, utcnow
from src.l1_agent.db.models import (
    TENANT_TABLES,
    api_tokens,
    login_states,
    metadata,
    role_mappings,
    runs,
    sessions,
    tenants,
    users,
    webhook_nonces,
)
from src.l1_agent.demo import DEMO_INCIDENTS
from src.l1_agent.models.incident import Incident
from src.l1_agent.service.runs import recent_records, record_by_number
from src.l1_agent.sop import ingest
from src.l1_agent.sop import store as sop_store
from tests.conftest import make_service

INTAKE = Actor.system("test-intake")


async def seed(pg, tenant: uuid.UUID, incident_index: int) -> str:
    """Put at least one row in every tenant table. Returns the incident number."""
    incident = Incident.from_servicenow(DEMO_INCIDENTS[incident_index])
    service = make_service(pg.db)
    await service.ingest(tenant, incident, INTAKE)
    await service.work_once([tenant])
    now = utcnow()
    later = now + timedelta(hours=1)
    async with pg.db.tenant(tenant) as conn:
        user_id = uuid.uuid4()
        await conn.execute(
            sa.insert(users).values(
                id=user_id, tenant_id=tenant, issuer="idp", subject=f"u-{tenant}", created_at=now
            )
        )
        await conn.execute(
            sa.insert(role_mappings).values(
                id=uuid.uuid4(), tenant_id=tenant, idp_group="ops", role="operator"
            )
        )
        await conn.execute(
            sa.insert(sessions).values(
                id=uuid.uuid4(),
                tenant_id=tenant,
                user_id=user_id,
                token_hash=uuid.uuid4().hex * 2,
                csrf_token="c",
                roles=["operator"],
                auth_method="oidc",
                created_at=now,
                last_seen_at=now,
                expires_at=later,
            )
        )
        await conn.execute(
            sa.insert(login_states).values(
                id=uuid.uuid4(),
                tenant_id=tenant,
                state_hash=uuid.uuid4().hex * 2,
                nonce="n",
                code_verifier="v",
                expires_at=later,
            )
        )
        await conn.execute(
            sa.insert(api_tokens).values(
                id=uuid.uuid4(),
                tenant_id=tenant,
                name="t",
                token_hash=uuid.uuid4().hex * 2,
                permissions=["incidents.read"],
                created_by="test",
                created_at=now,
                expires_at=later,
            )
        )
        await conn.execute(
            sa.insert(webhook_nonces).values(
                tenant_id=tenant, signature_hash=uuid.uuid4().hex * 2, expires_at=later
            )
        )
    await connections.set_connection(
        pg.db, tenant, INTAKE, "splunk", {"base_url": "https://splunk.example"}, {}
    )
    await sop_store.create_draft(pg.db, tenant, INTAKE, {"sop_id": "SOP-ISO", "steps": []})
    await ingest._record(pg.db, tenant, INTAKE, ingest.SyncResult("git"), "0" * 40)
    return incident.number


@pytest.fixture
async def two_tenants(pg):
    a, b = await pg.new_tenant("a"), await pg.new_tenant("b")
    number_a = await seed(pg, a, 1)
    number_b = await seed(pg, b, 0)
    return a, b, number_a, number_b


class TestSchemaCoversEveryTable:
    def test_every_table_but_tenants_carries_a_tenant(self):
        for table in metadata.sorted_tables:
            if table.name == "tenants":
                continue
            assert "tenant_id" in table.c, f"{table.name} has no tenant_id"
            assert not table.c.tenant_id.nullable or table.c.tenant_id.primary_key

    async def test_row_level_security_is_enabled_and_forced_on_every_tenant_table(self, pg):
        async with pg.db.platform() as conn:
            flags = {
                r.relname: (r.relrowsecurity, r.relforcerowsecurity)
                for r in await conn.execute(
                    sa.text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace"
                    )
                )
            }
            policies = {
                r.tablename
                for r in await conn.execute(
                    sa.text(
                        "SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation'"
                    )
                )
            }
        for table in TENANT_TABLES:
            assert flags[table.name] == (True, True), f"{table.name} is not protected"
            assert table.name in policies, f"{table.name} has no tenant policy"
        # Nothing exists in the database that the model, and so this test, does not know about.
        assert set(flags) - {"alembic_version"} == {t.name for t in metadata.sorted_tables}


class TestReadsWithNoFilter:
    @pytest.mark.parametrize("table", TENANT_TABLES, ids=lambda t: t.name)
    async def test_unfiltered_select_returns_only_the_current_tenant(self, pg, two_tenants, table):
        a, b, _, _ = two_tenants
        async with pg.db.tenant(a) as conn:
            seen_by_a = {r.tenant_id for r in await conn.execute(sa.select(table))}
        async with pg.db.tenant(b) as conn:
            seen_by_b = {r.tenant_id for r in await conn.execute(sa.select(table))}

        assert seen_by_a == {a}, f"{table.name}: tenant A saw {seen_by_a}"
        assert seen_by_b == {b}, "the other tenant has rows, so the check above is not vacuous"

    @pytest.mark.parametrize("table", TENANT_TABLES, ids=lambda t: t.name)
    async def test_no_tenant_set_sees_nothing(self, pg, two_tenants, table):
        async with pg.db.platform() as conn:
            assert (await conn.execute(sa.select(table))).all() == []

    async def test_raw_sql_and_joins_are_filtered_too(self, pg, two_tenants):
        a, b, _, number_b = two_tenants
        async with pg.db.tenant(a) as conn:
            numbers = [
                r[0]
                for r in await conn.execute(
                    sa.text(
                        "SELECT i.number FROM incidents i JOIN runs r ON r.incident_id = i.id "
                        "UNION SELECT number FROM runs"
                    )
                )
            ]
            by_id = await conn.execute(
                sa.text("SELECT count(*) FROM runs WHERE tenant_id = :b"), {"b": b}
            )
        assert number_b not in numbers
        assert by_id.scalar() == 0

    async def test_repository_reads_do_not_cross(self, pg, two_tenants):
        a, b, number_a, number_b = two_tenants
        assert {r["incident_number"] for r in await recent_records(pg.db, a)} == {number_a}
        assert await record_by_number(pg.db, a, number_b) is None
        assert (await record_by_number(pg.db, b, number_b))["incident_number"] == number_b

    async def test_setting_cannot_be_smuggled_through_a_fake_tenant_id(self, pg, two_tenants):
        with pytest.raises(TypeError):
            async with pg.db.tenant("00000000-0000-0000-0000-000000000000' OR '1'='1"):
                pass


class TestWritesAcrossTenants:
    async def test_insert_for_another_tenant_is_refused(self, pg, two_tenants):
        a, b, _, _ = two_tenants
        with pytest.raises(sa.exc.DBAPIError, match="row-level security"):
            async with pg.db.tenant(a) as conn:
                await conn.execute(
                    sa.insert(role_mappings).values(
                        id=uuid.uuid4(), tenant_id=b, idp_group="x", role="tenant_admin"
                    )
                )

    async def test_audit_event_for_another_tenant_is_refused(self, pg, two_tenants):
        a, b, _, _ = two_tenants
        with pytest.raises(sa.exc.DBAPIError, match="row-level security"):
            async with pg.db.tenant(a) as conn:
                await append(conn, b, INTAKE, "forged.event", {})

    @pytest.mark.parametrize(
        "table", [runs, sessions, api_tokens, role_mappings], ids=lambda t: t.name
    )
    async def test_update_and_delete_touch_no_row_of_another_tenant(self, pg, two_tenants, table):
        a, b, _, _ = two_tenants
        async with pg.db.tenant(b) as conn:
            before = (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()
        async with pg.db.tenant(a) as conn:
            updated = await conn.execute(
                sa.update(table).where(table.c.tenant_id == b).values(tenant_id=b)
            )
            deleted = await conn.execute(sa.delete(table).where(table.c.tenant_id == b))
        async with pg.db.tenant(b) as conn:
            after = (await conn.execute(sa.select(sa.func.count()).select_from(table))).scalar()

        assert updated.rowcount == 0 and deleted.rowcount == 0
        assert before == after and before > 0

    async def test_a_row_cannot_be_moved_to_another_tenant(self, pg, two_tenants):
        a, b, _, _ = two_tenants
        with pytest.raises(sa.exc.DBAPIError, match="row-level security"):
            async with pg.db.tenant(a) as conn:
                await conn.execute(sa.update(role_mappings).values(tenant_id=b))


class TestApplicationRole:
    async def test_application_role_passes_the_start_up_check(self, pg):
        await pg.db.check_role_is_safe()

    async def test_migration_role_is_refused_at_start_up(self, pg_urls):
        owner = Database.create(pg_urls.owner, demo_mode=False)
        try:
            with pytest.raises(UnsafeDatabaseRole, match="superuser"):
                await owner.check_role_is_safe()
        finally:
            await owner.close()

    async def test_application_role_cannot_create_or_change_a_tenant(self, pg, two_tenants):
        for statement in (
            sa.insert(tenants).values(id=uuid.uuid4(), slug="x", name="x", created_at=utcnow()),
            sa.update(tenants).values(status="disabled"),
            sa.delete(tenants),
        ):
            with pytest.raises(sa.exc.DBAPIError, match="permission denied"):
                async with pg.db.platform() as conn:
                    await conn.execute(statement)

    @pytest.mark.parametrize(
        "statements",
        [
            ["ALTER TABLE runs DISABLE ROW LEVEL SECURITY"],
            ["DROP POLICY tenant_isolation ON runs"],
            # With row_security off a query that a policy would filter is an error.
            ["SET LOCAL row_security = off", "SELECT * FROM runs"],
        ],
    )
    async def test_application_role_cannot_switch_row_security_off(
        self, pg, two_tenants, statements
    ):
        a, _, _, _ = two_tenants
        with pytest.raises(sa.exc.DBAPIError):
            async with pg.db.tenant(a) as conn:
                for statement in statements:
                    await conn.execute(sa.text(statement))

    def test_sqlite_is_refused_outside_demo_mode(self, tmp_path):
        from src.l1_agent.config.settings import ConfigError

        with pytest.raises(ConfigError, match="AGENT_DEMO_MODE"):
            Database.create(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/x.db", demo_mode=False)
        with pytest.raises(ConfigError, match="DATABASE_URL"):
            Database.create("", demo_mode=False)
