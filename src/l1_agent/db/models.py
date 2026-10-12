"""Database schema.

Every table except the two in GLOBAL_TABLES carries `tenant_id` and is listed in
TENANT_TABLES.
On PostgreSQL each of those has row level security (see the first migration),
so a query can only see rows of the tenant set on its transaction.
"""

from __future__ import annotations

from datetime import timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

Json = sa.JSON().with_variant(JSONB(), "postgresql")
BigSerial = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


class UtcDateTime(sa.TypeDecorator):
    """A timezone-aware UTC timestamp on every dialect.

    SQLite has no timezone type and hands back naive values; they are UTC.
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware")
        return value.astimezone(timezone.utc) if value is not None else None

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


Timestamp = UtcDateTime()


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False)


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), primary_key=True)


tenants = sa.Table(
    "tenants",
    metadata,
    _id(),
    sa.Column("slug", sa.String(64), nullable=False, unique=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    sa.Column("created_at", Timestamp, nullable=False),
)

users = sa.Table(
    "users",
    metadata,
    _id(),
    _tenant(),
    sa.Column("issuer", sa.String(500), nullable=False),
    sa.Column("subject", sa.String(500), nullable=False),
    sa.Column("email", sa.String(320), nullable=False, server_default=""),
    sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.Column("last_login_at", Timestamp),
    sa.UniqueConstraint("tenant_id", "issuer", "subject", name="uq_users_identity"),
)

role_mappings = sa.Table(
    "role_mappings",
    metadata,
    _id(),
    _tenant(),
    sa.Column("idp_group", sa.String(300), nullable=False),
    sa.Column("role", sa.String(40), nullable=False),
    sa.UniqueConstraint("tenant_id", "idp_group", "role", name="uq_role_mappings_pair"),
)

sessions = sa.Table(
    "sessions",
    metadata,
    _id(),
    _tenant(),
    sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("csrf_token", sa.String(64), nullable=False),
    sa.Column("roles", Json, nullable=False),
    sa.Column("auth_method", sa.String(20), nullable=False),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.Column("last_seen_at", Timestamp, nullable=False),
    sa.Column("expires_at", Timestamp, nullable=False),
    sa.Column("revoked_at", Timestamp),
)

login_states = sa.Table(
    "login_states",
    metadata,
    _id(),
    _tenant(),
    sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("nonce", sa.String(64), nullable=False),
    sa.Column("code_verifier", sa.String(128), nullable=False),
    sa.Column("expires_at", Timestamp, nullable=False),
)

api_tokens = sa.Table(
    "api_tokens",
    metadata,
    _id(),
    _tenant(),
    sa.Column("name", sa.String(100), nullable=False),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("permissions", Json, nullable=False),
    sa.Column("created_by", sa.String(200), nullable=False),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.Column("expires_at", Timestamp, nullable=False),
    sa.Column("revoked_at", Timestamp),
    sa.Column("last_used_at", Timestamp),
)

webhook_nonces = sa.Table(
    "webhook_nonces",
    metadata,
    sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), primary_key=True),
    sa.Column("signature_hash", sa.String(64), primary_key=True),
    sa.Column("expires_at", Timestamp, nullable=False),
)

incidents = sa.Table(
    "incidents",
    metadata,
    _id(),
    _tenant(),
    sa.Column("source", sa.String(40), nullable=False),
    sa.Column("external_id", sa.String(100), nullable=False),
    sa.Column("number", sa.String(64), nullable=False),
    sa.Column("payload", Json, nullable=False),
    sa.Column("received_at", Timestamp, nullable=False),
    # One row per ticket per source: the webhook and the poller both insert, one wins.
    sa.UniqueConstraint("tenant_id", "source", "external_id", name="uq_incidents_external"),
)

runs = sa.Table(
    "runs",
    metadata,
    _id(),
    _tenant(),
    sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id"), nullable=False),
    sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("number", sa.String(64), nullable=False),
    sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("state", sa.String(30), nullable=False),
    sa.Column("outcome", sa.String(20)),
    sa.Column("sop_id", sa.String(100), nullable=False, server_default=""),
    sa.Column("sop_version", sa.String(40), nullable=False, server_default=""),
    sa.Column("sop_hash", sa.String(64), nullable=False, server_default=""),
    sa.Column("confidence", sa.Float()),
    sa.Column("match_rationale", sa.Text(), nullable=False, server_default=""),
    # Parameter values a model located in the ticket (ADR 0009). Kept so that a
    # resumed run uses the same values and does not ask again.
    sa.Column("advised_parameters", Json),
    sa.Column("record", Json),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.Column("completed_at", Timestamp),
    sa.UniqueConstraint("incident_id", "generation", name="uq_runs_generation"),
    # At most one run without an outcome per incident.
    sa.Index(
        "uq_runs_one_open",
        "incident_id",
        unique=True,
        postgresql_where=sa.text("outcome IS NULL"),
        sqlite_where=sa.text("outcome IS NULL"),
    ),
    sa.Index("ix_runs_tenant_created", "tenant_id", "created_at"),
)

run_steps = sa.Table(
    "run_steps",
    metadata,
    _id(),
    _tenant(),
    sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
    sa.Column("seq", sa.Integer(), nullable=False),
    sa.Column("step_id", sa.String(100), nullable=False),
    sa.Column("status", sa.String(20), nullable=False),  # started | completed
    sa.Column("result", Json),
    sa.Column("started_at", Timestamp, nullable=False),
    sa.Column("completed_at", Timestamp),
    sa.UniqueConstraint("run_id", "step_id", name="uq_run_steps_step"),
)

jobs = sa.Table(
    "jobs",
    metadata,
    sa.Column("id", BigSerial, primary_key=True, autoincrement=True),
    _tenant(),
    sa.Column("kind", sa.String(40), nullable=False),
    sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
    sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    sa.Column("available_at", Timestamp, nullable=False),
    sa.Column("locked_until", Timestamp),
    sa.Column("locked_by", sa.String(100), nullable=False, server_default=""),
    sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.Index("ix_jobs_claim", "tenant_id", "status", "available_at"),
)

events = sa.Table(
    "events",
    metadata,
    sa.Column("id", BigSerial, primary_key=True, autoincrement=True),
    _tenant(),
    sa.Column("run_id", sa.Uuid()),
    sa.Column("type", sa.String(60), nullable=False),
    sa.Column("payload", Json, nullable=False),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.Index("ix_events_tenant_seq", "tenant_id", "id"),
)

# Append only. Time and payload are stored as the exact text that was hashed.
audit_events = sa.Table(
    "audit_events",
    metadata,
    sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), primary_key=True),
    sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=False),
    sa.Column("occurred_at", sa.String(40), nullable=False),
    sa.Column("actor_type", sa.String(20), nullable=False),
    sa.Column("actor_id", sa.String(300), nullable=False),
    sa.Column("correlation_id", sa.String(100), nullable=False, server_default=""),
    sa.Column("event_type", sa.String(80), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("prev_hash", sa.String(64), nullable=False),
    sa.Column("hash", sa.String(64), nullable=False),
    sa.Index("ix_audit_correlation", "tenant_id", "correlation_id"),
)

# A tenant's connection to one connector: settings, and references to credentials.
# A credential value is never stored here (ADR 0005).
connections = sa.Table(
    "connections",
    metadata,
    _id(),
    _tenant(),
    sa.Column("connector_id", sa.String(64), nullable=False),
    sa.Column("config", Json, nullable=False),
    sa.Column("secret_refs", Json, nullable=False),
    sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column("updated_by", sa.String(300), nullable=False),
    sa.Column("updated_at", Timestamp, nullable=False),
    sa.UniqueConstraint("tenant_id", "connector_id", name="uq_connections_connector"),
)

# How far each knowledge source has been read.
knowledge_sources = sa.Table(
    "knowledge_sources",
    metadata,
    sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), primary_key=True),
    sa.Column("source_id", sa.String(64), primary_key=True),
    sa.Column("cursor", sa.String(200), nullable=False, server_default=""),
    sa.Column("last_synced_at", Timestamp),
    sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
)

sops = sa.Table(
    "sops",
    metadata,
    _id(),
    _tenant(),
    sa.Column("sop_key", sa.String(100), nullable=False),
    sa.Column("title", sa.String(300), nullable=False, server_default=""),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.UniqueConstraint("tenant_id", "sop_key", name="uq_sops_key"),
)

SOP_DRAFT = "draft"
SOP_IN_REVIEW = "in_review"
SOP_APPROVED = "approved"
SOP_PUBLISHED = "published"
SOP_SUPERSEDED = "superseded"
SOP_RETIRED = "retired"
# From approval onwards the content cannot change and an approver is on record.
SOP_FROZEN = (SOP_APPROVED, SOP_PUBLISHED, SOP_SUPERSEDED, SOP_RETIRED)
_FROZEN_SQL = ", ".join(f"'{s}'" for s in SOP_FROZEN)

sop_versions = sa.Table(
    "sop_versions",
    metadata,
    _id(),
    _tenant(),
    sa.Column("sop_id", sa.Uuid(), sa.ForeignKey("sops.id"), nullable=False),
    sa.Column("version", sa.Integer()),  # assigned at publication
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("content", Json, nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
    sa.Column("origin", sa.String(20), nullable=False),  # authored | ingested | seed
    sa.Column("source_id", sa.String(64), nullable=False, server_default=""),
    sa.Column("source_article", sa.String(500), nullable=False, server_default=""),
    sa.Column("source_revision", sa.String(200), nullable=False, server_default=""),
    sa.Column("source_text", sa.Text(), nullable=False, server_default=""),
    sa.Column("author_id", sa.String(300), nullable=False),
    sa.Column("approver_id", sa.String(300)),
    sa.Column("review_note", sa.Text(), nullable=False, server_default=""),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.Column("updated_at", Timestamp, nullable=False),
    sa.Column("submitted_at", Timestamp),
    sa.Column("approved_at", Timestamp),
    sa.Column("published_at", Timestamp),
    sa.Column("retired_at", Timestamp),
    # The person who approves a version is never the person who wrote it.
    sa.CheckConstraint(
        "approver_id IS NULL OR approver_id <> author_id", name="ck_sop_versions_two_people"
    ),
    # Nothing is approved, published or retired without an approver and a content hash.
    sa.CheckConstraint(
        f"status NOT IN ({_FROZEN_SQL}) OR (approver_id IS NOT NULL AND content_hash <> '')",
        name="ck_sop_versions_approved_by_someone",
    ),
    sa.CheckConstraint(
        "status IN ('draft', 'in_review', " + _FROZEN_SQL + ")", name="ck_sop_versions_status"
    ),
    sa.UniqueConstraint("sop_id", "version", name="uq_sop_versions_number"),
    # One published version per SOP.
    sa.Index(
        "uq_sop_versions_one_published",
        "sop_id",
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
        sqlite_where=sa.text("status = 'published'"),
    ),
    # An article revision is turned into a draft once.
    sa.Index(
        "uq_sop_versions_source",
        "tenant_id",
        "source_id",
        "source_article",
        "source_revision",
        unique=True,
        postgresql_where=sa.text("source_id <> ''"),
        sqlite_where=sa.text("source_id <> ''"),
    ),
    sa.Index("ix_sop_versions_status", "tenant_id", "status"),
)

# Deployment-wide switches, set by the platform operator with the migration role.
# The application role can read this table and cannot write it. It holds no tenant data.
platform_flags = sa.Table(
    "platform_flags",
    metadata,
    sa.Column("name", sa.String(64), primary_key=True),
    sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("reason", sa.Text(), nullable=False, server_default=""),
    sa.Column("set_by", sa.String(300), nullable=False, server_default=""),
    sa.Column("set_at", Timestamp, nullable=False),
)
FLAG_KILL_SWITCH = "kill_switch"

AUTONOMY_LEVELS = ("shadow", "recommend", "approval", "automatic")

# One row per tenant: how far the agent may go, and the tenant's kill switch (ADR 0008).
policies = sa.Table(
    "policies",
    metadata,
    sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), primary_key=True),
    sa.Column("autonomy", sa.String(20), nullable=False, server_default="shadow"),
    sa.Column("rules", Json, nullable=False),
    sa.Column("kill_switch", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("kill_reason", sa.Text(), nullable=False, server_default=""),
    sa.Column("kill_set_by", sa.String(300), nullable=False, server_default=""),
    sa.Column("kill_set_at", Timestamp),
    sa.Column("updated_by", sa.String(300), nullable=False),
    sa.Column("updated_at", Timestamp, nullable=False),
    sa.CheckConstraint(
        "autonomy IN ('shadow', 'recommend', 'approval', 'automatic')", name="ck_policies_autonomy"
    ),
)

ACTION_STATUSES = (
    "denied",
    "shadowed",
    "recommended",
    "pending",  # waiting for an approval
    "approved",
    "rejected",
    "expired",
    "invalid",  # the approval or the decision no longer applies
    "allowed",
    "executing",  # the write has been authorised and may have been sent
    "executed",  # the write was accepted; not yet verified
    "not_done",  # a pre-check or dry run said no; nothing was changed
    "verified",
    "failed",  # the write or its verification failed, and nothing was rolled back
    "rolling_back",
    "rolled_back",
    "rollback_failed",
    "aborted",  # stopped by the kill switch
)
_ACTION_STATUS_SQL = ", ".join(f"'{s}'" for s in ACTION_STATUSES)

# One row per ACTION step of a run: the policy decision, the approval if one was
# needed, and what happened. It is also the approval request the approver sees.
actions = sa.Table(
    "actions",
    metadata,
    _id(),
    _tenant(),
    sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
    sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id"), nullable=False),
    sa.Column("incident_number", sa.String(64), nullable=False),
    sa.Column("step_id", sa.String(100), nullable=False),
    sa.Column("sop_key", sa.String(100), nullable=False),
    sa.Column("sop_version", sa.String(40), nullable=False, server_default=""),
    sa.Column("sop_hash", sa.String(64), nullable=False, server_default=""),
    sa.Column("sop_author_id", sa.String(300), nullable=False, server_default=""),
    sa.Column("capability_id", sa.String(120), nullable=False),
    sa.Column("parameters", Json, nullable=False),
    sa.Column("call_digest", sa.String(64), nullable=False),  # capability and parameters
    # tenant, incident, SOP version, capability and parameters: what an approval is for
    sa.Column("action_hash", sa.String(64), nullable=False),
    sa.Column("tier", sa.String(4), nullable=False),
    sa.Column("verdict", sa.String(20), nullable=False),
    sa.Column("reason_code", sa.String(60), nullable=False, server_default=""),
    sa.Column("rule", sa.String(300), nullable=False, server_default=""),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("change_template", sa.String(100), nullable=False, server_default=""),
    sa.Column("change_sys_id", sa.String(64), nullable=False, server_default=""),
    sa.Column("change_number", sa.String(40), nullable=False, server_default=""),
    sa.Column("channels", Json, nullable=False),  # where an approval may come from
    sa.Column("requested_at", Timestamp),
    sa.Column("expires_at", Timestamp),
    sa.Column("approver_id", sa.String(300)),
    sa.Column("approver_email", sa.String(320), nullable=False, server_default=""),
    sa.Column("approved_hash", sa.String(64), nullable=False, server_default=""),
    sa.Column("decided_via", sa.String(20), nullable=False, server_default=""),
    sa.Column("decision_note", sa.Text(), nullable=False, server_default=""),
    sa.Column("decided_at", Timestamp),
    sa.Column("valid_until", Timestamp),  # execution must begin before this
    sa.Column("execution_started_at", Timestamp),
    sa.Column("completed_at", Timestamp),
    sa.Column("result", Json),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.Column("updated_at", Timestamp, nullable=False),
    sa.UniqueConstraint("run_id", "step_id", name="uq_actions_step"),
    sa.CheckConstraint(f"status IN ({_ACTION_STATUS_SQL})", name="ck_actions_status"),
    sa.CheckConstraint("tier IN ('T1', 'T2')", name="ck_actions_tier"),
    sa.CheckConstraint(
        "verdict IN ('deny', 'shadow', 'recommend', 'require_approval', 'allow')",
        name="ck_actions_verdict",
    ),
    # Decision D5: nothing above T1 is ever allowed without an approval.
    sa.CheckConstraint("verdict <> 'allow' OR tier = 'T1'", name="ck_actions_only_t1_unattended"),
    # An approval is of exactly this action, by a named person.
    sa.CheckConstraint(
        "status <> 'approved' OR (approver_id IS NOT NULL AND approved_hash = action_hash)",
        name="ck_actions_approval_is_for_this_action",
    ),
    # For T2 the approver is never the author of the SOP version.
    sa.CheckConstraint(
        "tier <> 'T2' OR approver_id IS NULL OR approver_id <> sop_author_id",
        name="ck_actions_t2_two_people",
    ),
    # Nothing starts executing unless policy allowed it, or required an approval and got one.
    sa.CheckConstraint(
        "execution_started_at IS NULL OR verdict = 'allow' OR "
        "(verdict = 'require_approval' AND approver_id IS NOT NULL AND approved_hash = action_hash)",
        name="ck_actions_executed_was_authorised",
    ),
    # The same write is made at most once for an incident, across all its runs.
    sa.Index(
        "uq_actions_once_per_incident",
        "incident_id",
        "call_digest",
        unique=True,
        postgresql_where=sa.text("execution_started_at IS NOT NULL"),
        sqlite_where=sa.text("execution_started_at IS NOT NULL"),
    ),
    sa.Index("ix_actions_status", "tenant_id", "status"),
)

# ── Model use (ADR 0009) ──────────────────────────────────────────────

# Whether a tenant uses a model at all, with which providers, for which tasks,
# and within which budgets. No row means no model.
model_policies = sa.Table(
    "model_policies",
    metadata,
    sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), primary_key=True),
    sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("rules", Json, nullable=False),
    sa.Column("updated_by", sa.String(300), nullable=False),
    sa.Column("updated_at", Timestamp, nullable=False),
)

MODEL_CALL_OUTCOMES = ("ok", "invalid", "error", "over_budget")

# One row per request to a provider, and one per request that a budget stopped.
# Digests and counts only: neither the prompt nor the answer is stored.
model_calls = sa.Table(
    "model_calls",
    metadata,
    _id(),
    _tenant(),
    sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id")),  # null: not part of a run
    sa.Column("subject", sa.String(200), nullable=False, server_default=""),
    sa.Column("task", sa.String(20), nullable=False),
    sa.Column("provider", sa.String(40), nullable=False),
    sa.Column("model", sa.String(200), nullable=False),
    sa.Column("prompt_version", sa.String(40), nullable=False),
    sa.Column("input_digest", sa.String(64), nullable=False),
    sa.Column("output_digest", sa.String(64), nullable=False, server_default=""),
    sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("cost_micros", sa.BigInteger(), nullable=False, server_default="0"),
    sa.Column("outcome", sa.String(20), nullable=False),
    sa.Column("detail", sa.String(300), nullable=False, server_default=""),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.CheckConstraint(
        "outcome IN (" + ", ".join(f"'{o}'" for o in MODEL_CALL_OUTCOMES) + ")",
        name="ck_model_calls_outcome",
    ),
    sa.CheckConstraint(
        "input_tokens >= 0 AND output_tokens >= 0 AND cost_micros >= 0",
        name="ck_model_calls_not_negative",
    ),
    sa.Index("ix_model_calls_tenant_created", "tenant_id", "created_at"),
    sa.Index("ix_model_calls_run", "tenant_id", "run_id"),
)

# ── The runner (ADR 0010) ─────────────────────────────────────────────

# A runner a tenant administrator has enrolled: its name and its public key.
# The private key never leaves the runner.
runners = sa.Table(
    "runners",
    metadata,
    _id(),
    _tenant(),
    sa.Column("name", sa.String(100), nullable=False),
    sa.Column("public_key", sa.String(64), nullable=False),  # Ed25519, base64
    sa.Column("enrolled_by", sa.String(300), nullable=False),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.Column("revoked_at", Timestamp),
    sa.Column("last_seen_at", Timestamp),
    # What the runner says about itself when it asks for work: version and local ceiling.
    sa.Column("reported", Json),
    sa.UniqueConstraint("tenant_id", "name", name="uq_runners_name"),
    # A runner may name itself by its key (ADR 0010), so a key belongs to one runner.
    sa.UniqueConstraint("tenant_id", "public_key", name="uq_runners_key"),
)

RUNNER_CALL_STATUSES = ("queued", "claimed", "done", "cancelled")

# One connector call waiting for, held by, or answered by a runner. A row is
# removed when its answer has been read, so results are not kept here.
runner_calls = sa.Table(
    "runner_calls",
    metadata,
    _id(),
    _tenant(),
    sa.Column("kind", sa.String(20), nullable=False),  # invoke | dry_run | test
    sa.Column("capability_id", sa.String(120), nullable=False, server_default=""),
    sa.Column("connector_id", sa.String(64), nullable=False),
    sa.Column("is_write", sa.Boolean(), nullable=False, server_default=sa.false()),
    # The exact text the control plane signed, and its signature. No credential is in it.
    sa.Column("envelope", sa.Text(), nullable=False),
    sa.Column("signature", sa.String(128), nullable=False),
    sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
    sa.Column("runner_id", sa.Uuid(), sa.ForeignKey("runners.id")),
    sa.Column("created_at", Timestamp, nullable=False),
    sa.Column("deadline", Timestamp, nullable=False),
    sa.Column("claimed_at", Timestamp),
    sa.Column("completed_at", Timestamp),
    sa.Column("outcome", sa.String(20), nullable=False, server_default=""),
    sa.Column("result", Json),
    sa.CheckConstraint(
        "status IN (" + ", ".join(f"'{s}'" for s in RUNNER_CALL_STATUSES) + ")",
        name="ck_runner_calls_status",
    ),
    sa.Index("ix_runner_calls_claim", "tenant_id", "status", "created_at"),
)

GLOBAL_TABLES = ("tenants", "platform_flags")
TENANT_TABLES = tuple(t for t in metadata.sorted_tables if t.name not in GLOBAL_TABLES)
APPEND_ONLY_TABLES = ("audit_events",)

# The only SOPs the engine may match and run. A view, so the rule is in the database
# and not only in a query someone could write differently (docs/target-architecture.md §5).
PUBLISHED_SOPS_VIEW = "published_sops"
PUBLISHED_SOPS_SELECT = (
    "SELECT v.id AS id, v.tenant_id AS tenant_id, s.sop_key AS sop_key, v.version AS version, "
    "v.content AS content, v.content_hash AS content_hash "
    "FROM sop_versions v JOIN sops s ON s.id = v.sop_id WHERE v.status = 'published'"
)
_views = sa.MetaData()
published_sops = sa.Table(
    PUBLISHED_SOPS_VIEW,
    _views,
    sa.Column("id", sa.Uuid()),
    sa.Column("tenant_id", sa.Uuid()),
    sa.Column("sop_key", sa.String(100)),
    sa.Column("version", sa.Integer()),
    sa.Column("content", Json),
    sa.Column("content_hash", sa.String(64)),
)
