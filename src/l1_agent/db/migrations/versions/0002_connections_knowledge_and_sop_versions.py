"""Connections, knowledge sources and the SOP version store.

Revision ID: 0002
Revises: 0001
"""

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TENANT_TABLES = ("connections", "knowledge_sources", "sops", "sop_versions")
CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
FROZEN = "('approved', 'published', 'superseded', 'retired')"


def _secure() -> None:
    """Row level security and grants for the new tables, and the SOP version guard."""
    role = op.get_context().config.attributes.get("app_role", "l1_app")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise RuntimeError("The application role name must be a plain lower-case identifier")
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {CURRENT_TENANT}) WITH CHECK (tenant_id = {CURRENT_TENANT})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")

    # The engine reads SOPs through this view only. security_invoker makes the
    # caller's row level security apply to it (PostgreSQL 15 or later).
    op.execute(
        "CREATE VIEW published_sops WITH (security_invoker = true) AS "
        "SELECT v.id AS id, v.tenant_id AS tenant_id, s.sop_key AS sop_key, "
        "v.version AS version, v.content AS content, v.content_hash AS content_hash "
        "FROM sop_versions v JOIN sops s ON s.id = v.sop_id WHERE v.status = 'published'"
    )
    op.execute(f"GRANT SELECT ON published_sops TO {role}")

    # What a version says, who wrote it and who approved it are fixed once it has
    # been submitted. Only a draft can be edited or deleted, by anyone, the owner included.
    op.execute(
        f"""
        CREATE FUNCTION sop_versions_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status <> 'draft' THEN
                    RAISE EXCEPTION 'only a draft SOP version can be deleted';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.status <> 'draft' AND (
                NEW.content IS DISTINCT FROM OLD.content
                OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                OR NEW.author_id IS DISTINCT FROM OLD.author_id
                OR NEW.sop_id IS DISTINCT FROM OLD.sop_id
                OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
            ) THEN
                RAISE EXCEPTION 'a submitted SOP version cannot be changed';
            END IF;
            IF OLD.status IN {FROZEN} AND (
                NEW.approver_id IS DISTINCT FROM OLD.approver_id
                OR NEW.status NOT IN {FROZEN}
                OR (OLD.version IS NOT NULL AND NEW.version IS DISTINCT FROM OLD.version)
            ) THEN
                RAISE EXCEPTION 'an approved SOP version cannot be changed';
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER sop_versions_immutable BEFORE UPDATE OR DELETE ON sop_versions "
        "FOR EACH ROW EXECUTE FUNCTION sop_versions_guard()"
    )


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.String(length=64), nullable=False),
        sa.Column(
            "config",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "secret_refs",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_by", sa.String(length=300), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_connections_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connections")),
        sa.UniqueConstraint("tenant_id", "connector_id", name="uq_connections_connector"),
    )
    op.create_table(
        "knowledge_sources",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("cursor", sa.String(length=200), server_default="", nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), server_default="", nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_knowledge_sources_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("tenant_id", "source_id", name=op.f("pk_knowledge_sources")),
    )
    op.create_table(
        "sops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sop_key", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=300), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_sops_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sops")),
        sa.UniqueConstraint("tenant_id", "sop_key", name="uq_sops_key"),
    )
    op.create_table(
        "sop_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sop_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "content",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("source_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("source_article", sa.String(length=500), server_default="", nullable=False),
        sa.Column("source_revision", sa.String(length=200), server_default="", nullable=False),
        sa.Column("source_text", sa.Text(), server_default="", nullable=False),
        sa.Column("author_id", sa.String(length=300), nullable=False),
        sa.Column("approver_id", sa.String(length=300), nullable=True),
        sa.Column("review_note", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'in_review', 'approved', 'published', 'superseded', 'retired')",
            name="ck_sop_versions_status",
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'published', 'superseded', 'retired') OR (approver_id IS NOT NULL AND content_hash <> '')",
            name="ck_sop_versions_approved_by_someone",
        ),
        sa.CheckConstraint(
            "approver_id IS NULL OR approver_id <> author_id", name="ck_sop_versions_two_people"
        ),
        sa.ForeignKeyConstraint(["sop_id"], ["sops.id"], name=op.f("fk_sop_versions_sop_id_sops")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_sop_versions_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sop_versions")),
        sa.UniqueConstraint("sop_id", "version", name="uq_sop_versions_number"),
    )
    op.create_index("ix_sop_versions_status", "sop_versions", ["tenant_id", "status"], unique=False)
    op.create_index(
        "uq_sop_versions_one_published",
        "sop_versions",
        ["sop_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
        sqlite_where=sa.text("status = 'published'"),
    )
    op.create_index(
        "uq_sop_versions_source",
        "sop_versions",
        ["tenant_id", "source_id", "source_article", "source_revision"],
        unique=True,
        postgresql_where=sa.text("source_id <> ''"),
        sqlite_where=sa.text("source_id <> ''"),
    )
    op.add_column(
        "runs", sa.Column("sop_hash", sa.String(length=64), server_default="", nullable=False)
    )
    _secure()


def downgrade() -> None:
    raise RuntimeError("Migrations are forward only")
