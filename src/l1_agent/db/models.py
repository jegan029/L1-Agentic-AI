"""Database schema.

Every table except `tenants` carries `tenant_id` and is listed in TENANT_TABLES.
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
    sa.Column("confidence", sa.Float()),
    sa.Column("match_rationale", sa.Text(), nullable=False, server_default=""),
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

TENANT_TABLES = tuple(t for t in metadata.sorted_tables if t.name != "tenants")
APPEND_ONLY_TABLES = ("audit_events",)
