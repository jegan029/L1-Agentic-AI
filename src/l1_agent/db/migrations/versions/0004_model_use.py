"""Model use: tenant model policy, the record of every model call, advised parameters (ADR 0009).

Revision ID: 0004
Revises: 0003
"""

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TENANT_TABLES = ("model_policies", "model_calls")
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
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON model_policies TO {role}")
    # The record of model calls is append only: budgets are counted from it.
    op.execute(f"GRANT SELECT, INSERT ON model_calls TO {role}")
    op.execute(
        """
        CREATE FUNCTION model_calls_guard() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'the record of a model call cannot be changed or deleted';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER model_calls_immutable BEFORE UPDATE OR DELETE ON model_calls "
        "FOR EACH ROW EXECUTE FUNCTION model_calls_guard()"
    )


def upgrade() -> None:
    op.add_column("runs", sa.Column("advised_parameters", _json(), nullable=True))
    op.create_table(
        "model_policies",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rules", _json(), nullable=False),
        sa.Column("updated_by", sa.String(length=300), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_model_policies_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_model_policies")),
    )
    op.create_table(
        "model_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("subject", sa.String(length=200), server_default="", nullable=False),
        sa.Column("task", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("output_digest", sa.String(length=64), server_default="", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.String(length=300), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('ok', 'invalid', 'error', 'over_budget')", name="ck_model_calls_outcome"
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_micros >= 0",
            name="ck_model_calls_not_negative",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_model_calls_run_id_runs")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_model_calls_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_calls")),
    )
    op.create_index(
        "ix_model_calls_tenant_created", "model_calls", ["tenant_id", "created_at"], unique=False
    )
    op.create_index("ix_model_calls_run", "model_calls", ["tenant_id", "run_id"], unique=False)
    _secure()


def downgrade() -> None:
    raise RuntimeError("Migrations are forward only")
