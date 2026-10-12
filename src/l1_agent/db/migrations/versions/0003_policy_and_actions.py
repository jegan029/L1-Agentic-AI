"""Tenant policy, the kill switches and the record of every action (ADR 0008).

Revision ID: 0003
Revises: 0002
"""

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TENANT_TABLES = ("policies", "actions")
CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
STATUSES = (
    "'denied', 'shadowed', 'recommended', 'pending', 'approved', 'rejected', 'expired', "
    "'invalid', 'allowed', 'executing', 'executed', 'not_done', 'verified', 'failed', "
    "'rolling_back', 'rolled_back', 'rollback_failed', 'aborted'"
)


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
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON policies TO {role}")
    # An action record is never deleted.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON actions TO {role}")
    # The global kill switch: the application reads it, only the platform operator sets it.
    op.execute(f"GRANT SELECT ON platform_flags TO {role}")

    # What was decided, and what an approval was given for, cannot be rewritten afterwards.
    op.execute(
        """
        CREATE FUNCTION actions_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'an action record cannot be deleted';
            END IF;
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                OR NEW.run_id IS DISTINCT FROM OLD.run_id
                OR NEW.incident_id IS DISTINCT FROM OLD.incident_id
                OR NEW.step_id IS DISTINCT FROM OLD.step_id
                OR NEW.capability_id IS DISTINCT FROM OLD.capability_id
                OR NEW.parameters IS DISTINCT FROM OLD.parameters
                OR NEW.call_digest IS DISTINCT FROM OLD.call_digest
                OR NEW.action_hash IS DISTINCT FROM OLD.action_hash
                OR NEW.sop_key IS DISTINCT FROM OLD.sop_key
                OR NEW.sop_hash IS DISTINCT FROM OLD.sop_hash
                OR NEW.sop_author_id IS DISTINCT FROM OLD.sop_author_id
                OR NEW.tier IS DISTINCT FROM OLD.tier
            THEN
                RAISE EXCEPTION 'what an action is cannot be changed after the decision';
            END IF;
            IF NEW.verdict IS DISTINCT FROM OLD.verdict AND NEW.verdict <> 'deny' THEN
                RAISE EXCEPTION 'a decision can only be changed to a denial';
            END IF;
            IF OLD.approver_id IS NOT NULL AND (
                NEW.approver_id IS DISTINCT FROM OLD.approver_id
                OR NEW.approved_hash IS DISTINCT FROM OLD.approved_hash
                OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
            ) THEN
                RAISE EXCEPTION 'an approval decision cannot be changed';
            END IF;
            IF OLD.execution_started_at IS NOT NULL
                AND NEW.execution_started_at IS DISTINCT FROM OLD.execution_started_at
            THEN
                RAISE EXCEPTION 'the start of an execution cannot be changed';
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER actions_immutable BEFORE UPDATE OR DELETE ON actions "
        "FOR EACH ROW EXECUTE FUNCTION actions_guard()"
    )


def upgrade() -> None:
    op.create_table(
        "platform_flags",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("set_by", sa.String(length=300), server_default="", nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_platform_flags")),
    )
    op.create_table(
        "policies",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("autonomy", sa.String(length=20), server_default="shadow", nullable=False),
        sa.Column("rules", _json(), nullable=False),
        sa.Column("kill_switch", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("kill_reason", sa.Text(), server_default="", nullable=False),
        sa.Column("kill_set_by", sa.String(length=300), server_default="", nullable=False),
        sa.Column("kill_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(length=300), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "autonomy IN ('shadow', 'recommend', 'approval', 'automatic')",
            name="ck_policies_autonomy",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_policies_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_policies")),
    )
    op.create_table(
        "actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("incident_number", sa.String(length=64), nullable=False),
        sa.Column("step_id", sa.String(length=100), nullable=False),
        sa.Column("sop_key", sa.String(length=100), nullable=False),
        sa.Column("sop_version", sa.String(length=40), server_default="", nullable=False),
        sa.Column("sop_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("sop_author_id", sa.String(length=300), server_default="", nullable=False),
        sa.Column("capability_id", sa.String(length=120), nullable=False),
        sa.Column("parameters", _json(), nullable=False),
        sa.Column("call_digest", sa.String(length=64), nullable=False),
        sa.Column("action_hash", sa.String(length=64), nullable=False),
        sa.Column("tier", sa.String(length=4), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=60), server_default="", nullable=False),
        sa.Column("rule", sa.String(length=300), server_default="", nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("change_template", sa.String(length=100), server_default="", nullable=False),
        sa.Column("change_sys_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("change_number", sa.String(length=40), server_default="", nullable=False),
        sa.Column("channels", _json(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approver_id", sa.String(length=300), nullable=True),
        sa.Column("approver_email", sa.String(length=320), server_default="", nullable=False),
        sa.Column("approved_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("decided_via", sa.String(length=20), server_default="", nullable=False),
        sa.Column("decision_note", sa.Text(), server_default="", nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", _json(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_actions_status"),
        sa.CheckConstraint("tier IN ('T1', 'T2')", name="ck_actions_tier"),
        sa.CheckConstraint(
            "verdict IN ('deny', 'shadow', 'recommend', 'require_approval', 'allow')",
            name="ck_actions_verdict",
        ),
        sa.CheckConstraint(
            "verdict <> 'allow' OR tier = 'T1'", name="ck_actions_only_t1_unattended"
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR (approver_id IS NOT NULL AND approved_hash = action_hash)",
            name="ck_actions_approval_is_for_this_action",
        ),
        sa.CheckConstraint(
            "tier <> 'T2' OR approver_id IS NULL OR approver_id <> sop_author_id",
            name="ck_actions_t2_two_people",
        ),
        sa.CheckConstraint(
            "execution_started_at IS NULL OR verdict = 'allow' OR "
            "(verdict = 'require_approval' AND approver_id IS NOT NULL "
            "AND approved_hash = action_hash)",
            name="ck_actions_executed_was_authorised",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], name=op.f("fk_actions_incident_id_incidents")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_actions_run_id_runs")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_actions_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_actions")),
        sa.UniqueConstraint("run_id", "step_id", name="uq_actions_step"),
    )
    op.create_index("ix_actions_status", "actions", ["tenant_id", "status"], unique=False)
    op.create_index(
        "uq_actions_once_per_incident",
        "actions",
        ["incident_id", "call_digest"],
        unique=True,
        postgresql_where=sa.text("execution_started_at IS NOT NULL"),
        sqlite_where=sa.text("execution_started_at IS NOT NULL"),
    )
    _secure()


def downgrade() -> None:
    raise RuntimeError("Migrations are forward only")
