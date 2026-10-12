"""The runner: enrolled runners and the queue of connector calls to them (ADR 0010).

Revision ID: 0005
Revises: 0004
"""

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TENANT_TABLES = ("runners", "runner_calls")
CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _secure() -> None:
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
    # A runner is revoked, never deleted: its id stays in the audit log.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON runners TO {role}")
    # A call is removed once its answer has been read.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON runner_calls TO {role}")
    # What a call is cannot be changed after it was signed; only its progress can.
    op.execute(
        """
        CREATE FUNCTION runner_calls_guard() RETURNS trigger AS $$
        BEGIN
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                OR NEW.kind IS DISTINCT FROM OLD.kind
                OR NEW.capability_id IS DISTINCT FROM OLD.capability_id
                OR NEW.connector_id IS DISTINCT FROM OLD.connector_id
                OR NEW.is_write IS DISTINCT FROM OLD.is_write
                OR NEW.envelope IS DISTINCT FROM OLD.envelope
                OR NEW.signature IS DISTINCT FROM OLD.signature
                OR NEW.deadline IS DISTINCT FROM OLD.deadline
            THEN
                RAISE EXCEPTION 'a call to a runner cannot be changed after it was signed';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER runner_calls_immutable BEFORE UPDATE ON runner_calls "
        "FOR EACH ROW EXECUTE FUNCTION runner_calls_guard()"
    )


def upgrade() -> None:
    op.create_table(
        "runners",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("public_key", sa.String(length=64), nullable=False),
        sa.Column("enrolled_by", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported", _json(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_runners_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runners")),
        sa.UniqueConstraint("tenant_id", "name", name="uq_runners_name"),
        sa.UniqueConstraint("tenant_id", "public_key", name="uq_runners_key"),
    )
    op.create_table(
        "runner_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("capability_id", sa.String(length=120), server_default="", nullable=False),
        sa.Column("connector_id", sa.String(length=64), nullable=False),
        sa.Column("is_write", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("envelope", sa.Text(), nullable=False),
        sa.Column("signature", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("runner_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=20), server_default="", nullable=False),
        sa.Column("result", _json(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'done', 'cancelled')", name="ck_runner_calls_status"
        ),
        sa.ForeignKeyConstraint(
            ["runner_id"], ["runners.id"], name=op.f("fk_runner_calls_runner_id_runners")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_runner_calls_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runner_calls")),
    )
    op.create_index(
        "ix_runner_calls_claim",
        "runner_calls",
        ["tenant_id", "status", "created_at"],
        unique=False,
    )
    _secure()


def downgrade() -> None:
    raise RuntimeError("Migrations are forward only")
