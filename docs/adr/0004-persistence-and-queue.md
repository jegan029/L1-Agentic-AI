# ADR 0004: Persistence, job queue and idempotency

Status: accepted and implemented in Phase 2, with the differences listed at the end.

## Context

State is an in-memory set, an in-memory deque and two JSONL files. Incidents accepted by the webhook are held only as unreferenced asyncio tasks. A restart loses work in progress and the idempotency record, and two replicas would both process the same incident.

## Decision

### Database

PostgreSQL 15 or later. SQLAlchemy 2 (async) for access, Alembic for migrations. Migrations are forward-only in production, reviewed, and run as a separate job before the application starts.

Tenant isolation uses row level security on every tenant-scoped table, with the tenant set per transaction through a session setting. The application connects as a role that does not own the tables and cannot bypass RLS. A separate migration role owns the schema. Separate schemas per tenant were considered and rejected: migrations and connection pooling become per-tenant operations, and shape A has only one tenant anyway.

### Job queue

A queue table in the same PostgreSQL database, with workers claiming jobs using `SELECT ... FOR UPDATE SKIP LOCKED`, visibility timeouts, attempt counts and a dead-letter state. Use the `procrastinate` library if it passes review in Phase 2; otherwise a small in-house implementation of the same pattern.

The reason for PostgreSQL instead of a broker: a state transition and the enqueue of its next job commit in one transaction (transactional outbox). There is no window where the state says "executing" and no job exists, or the reverse. It also means the customer runs one stateful service instead of two.

### Idempotency

- `incidents`: unique on `(tenant_id, itsm_connection_id, external_id)`. Webhook and poller both insert with `ON CONFLICT DO NOTHING`; the one that inserts enqueues the run.
- `runs`: a partial unique index allows one non-terminal run per incident. A reopened incident creates a new run with an incremented generation.
- `action_executions`: unique on `(run_id, step_id, attempt)` and a separate unique on the action's idempotency key, so a retried job cannot send a second force start.
- The ServiceNow claim step (assign to the agent's user and move to In Progress, conditional on the current state) is a second, external guard and does not replace the constraint.

### Step execution semantics

A step is recorded as started before the connector call and completed after. On recovery, a started read step is re-run. A started write step is never re-run blindly: the engine runs the capability's verify operation to find out whether the write took effect, then continues or goes to NEEDS_HUMAN.

### Demo mode and tests

Demo mode must keep running without infrastructure. The repository layer has one production implementation (PostgreSQL). Demo mode uses the same SQLAlchemy models on SQLite with a single fixed tenant. Start-up refuses SQLite unless demo mode is on. Tenant isolation, queue concurrency and constraint tests run only against real PostgreSQL in CI through a service container. This is two dialects, which is a cost; it is accepted to keep `python -m src.l1_agent.demo` a one-command experience.

## Consequences

- Throughput is bounded by PostgreSQL. Incident volumes for this product are thousands per day per tenant at most, far below the limit of this pattern.
- Dashboard endpoints move from in-memory aggregation to SQL queries; `store/incident_history.py` and `store/sop_stats.py` are rewritten against the run table. They are 200 lines together.
- Server-sent events need a cross-process source. Use PostgreSQL `LISTEN/NOTIFY` fed from the outbox, with the event bus interface unchanged.

## Alternatives considered

- Redis plus Celery or RQ. Rejected: a second stateful dependency in the customer environment and no transactional coupling with state.
- Temporal. A good fit for durable workflows, but a heavy platform for a customer to operate, and it would hold run history outside our audited store.
- Kafka. Not justified at this volume.

## As built in Phase 2

- The queue is the in-house implementation (`service/runs.py`), about 150 lines. `procrastinate` was not adopted: it brings its own schema and migrations, which would sit outside the row level security and audit design here.
- Access is SQLAlchemy Core, not the ORM. Queries are few and explicit.
- Server-sent events read an `events` table by polling once a second. `LISTEN/NOTIFY` was not used: polling is the same code on both dialects and survives a connection pooler.
- `runs.generation` exists but nothing reopens an incident yet. `action_executions` and the ServiceNow claim step arrive with Phases 3 and 4.
- The idempotency key is `(tenant_id, source, external_id)`; `source` stands in for the ITSM connection id until connections are records (Phase 3).
- The application role is a member of a group role, `l1_app`, created by the first migration. The service checks at start-up that it is not connected as a role that can bypass isolation.
- A resumed run replays recorded step results. A crash during the final ITSM update can repeat the closing work notes; that update is not journalled step by step.
