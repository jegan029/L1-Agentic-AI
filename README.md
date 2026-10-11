# L1 Engineer Agent

An async Python service that takes a ServiceNow incident, matches it to a published Standard Operating Procedure (SOP), runs the SOP's diagnostic steps through tool adapters, and ends in one explicit outcome: resolved with verification, or handed to a person with the evidence. A React dashboard shows what the agent did.

**Status: pre-release.** This is a working demo being turned into a product. The ServiceNow client has run against one developer instance and the model client against one hosted provider. Every tool adapter (Autosys, Splunk, Dynatrace, IBM MQ, mainframe, file shares, browser) has only run against a mock, and sign-in has only run against identity providers faked in the tests. It holds no security or compliance certification. The plan, architecture, threat model and controls matrix are in [docs/](docs/).

## What it does today

- Receives incidents by signed webhook or by polling ServiceNow.
- Matches each incident to an SOP by keyword, CI, category and assignment group. A field the ticket does not carry is left out of the score; it is not counted as a mismatch. A model, if configured, can help choose between candidates that already clear the confidence threshold. It has no tools and cannot decide what runs.
- Binds typed parameters from the ticket (for example an Autosys job name), validates them, and stops if any cannot be bound.
- Executes the SOP step by step with read-only adapters: Splunk, IR360/IBM MQ, Autosys, Windows file shares, Dynatrace, a browser check and mainframe TN3270.
- Ends every run in one of four outcomes.

| Outcome | Meaning |
|---|---|
| `resolved` | A VERIFY step ran and its assertion showed the symptom is gone. The incident is resolved in ServiceNow. |
| `escalated` | The SOP routed the incident to an L2 team, with the evidence collected. |
| `needs_human` | The agent could not continue safely: invalid SOP, unbound parameter, adapter unavailable, internal error. The incident stays open and is routed to a person. |
| `aborted` | Stopped by an operator. Reserved; nothing triggers it yet. |

There is no default outcome. A run cannot be recorded as resolved without a passed verification; see [ADR 0002](docs/adr/0002-fail-closed-outcomes.md).

All adapters are read only. The agent does not change anything in a target system yet. Remediation actions with policy and approval are planned for a later phase.

## Quick start (demo mode)

Requires Python 3.11+ and Node 18+.

```bash
pip install -r requirements-dev.txt

# One-shot demo: five sample incidents through the real engine, with mocks
python -m src.l1_agent.demo

# Service in demo mode: mock adapters, sample SOPs, no credentials, loopback only
AGENT_DEMO_MODE=true LLM_ENABLED=false python -m src.l1_agent.main
```

Dashboard, in a second terminal:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api/* to 127.0.0.1:8080
```

Send an incident to the demo service:

```bash
curl -X POST http://127.0.0.1:8080/webhook/incident \
  -H "Content-Type: application/json" \
  -d '{
    "sys_id": "demo-mq-002",
    "number": "INC0009005",
    "short_description": "MQ queue depth high on ORDER.REQUEST",
    "description": "Queue depth alert for ORDER.REQUEST on queue manager QMPROD01.",
    "category": "Middleware",
    "subcategory": "MQ",
    "cmdb_ci": "OrderService",
    "assignment_group": "L1-Middleware-Support",
    "priority": "3",
    "state": "1"
  }'
```

What the sample scenarios do against the mocks:

| Incident | SOP | Outcome | Why |
|---|---|---|---|
| MQ depth high on `ORDER.REQUEST` | SOP-MQ-001 | resolved | The mock reports the queue drained; verification passes |
| MQ depth high on `PAYMENT.REQUEST` | SOP-MQ-001 | escalated | Depth still 1,523; verification fails |
| Autosys job `BATCH_PAYMENT_PROCESS` failed | SOP-AUTOSYS-001 | escalated | Job still in FA; a restart needs a person |
| High memory on `app-server-prod01` | SOP-INFRA-001 | escalated | Memory still at 92.5 percent |
| SSL certificate expiring | none | escalated | No SOP clears the confidence threshold |

## Running outside demo mode

Outside demo mode the service needs PostgreSQL, a webhook secret and, for people to sign in, an identity provider. It refuses to start in an unsafe configuration. [RUNBOOK.md](RUNBOOK.md) has the full set-up; in short:

```bash
cp .env.example .env     # then fill in values; never commit .env
pip install -r requirements.txt -r requirements-connectors.txt   # add requirements-saml.txt for SAML
l1-agent migrate                                   # as the migration role
l1-agent tenant create --slug default
l1-agent roles grant --group <idp group> --role tenant_admin
l1-agent serve
```

| Area | What applies |
|---|---|
| Database | PostgreSQL 15+. The service connects as a restricted role; row level security separates tenants. SQLite is demo mode only. |
| Sign-in | OpenID Connect or SAML 2.0. No local passwords, except one optional break-glass account whose every use is audited. |
| Roles | Viewer, operator, approver, SOP author, SOP approver, tenant administrator, auditor, assigned from identity provider groups. |
| Scripts | Scoped API tokens (`l1-agent token create`), read permissions only. |
| Webhook | HMAC signature with a timestamp; replays are rejected. |
| Audit | Append-only, hash-chained log; `l1-agent audit verify`. |

Webhook callers sign each request:

```
X-L1-Timestamp: <unix seconds>
X-L1-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>" with AGENT_WEBHOOK_SECRET>
```

Terminate TLS in front of the service; it does not serve HTTPS itself, and the session cookie is only sent over HTTPS.

TLS verification towards Splunk, Dynatrace and IR360 is always on, and the mainframe connection uses TLS. The switches that turn these off are accepted only in demo mode.

The dashboard signs in through the service and must be served from the same origin as the API. In development the Vite proxy does that. Branding is set with the `VITE_BRAND_*` variables in `frontend/.env.example`.

## Writing an SOP

SOPs are JSON. The samples in [data/sample_sops/](data/sample_sops/) show the full shape. The rules the validator enforces:

- At least one step; every `on_success` and `on_failure` points at a step that exists; no cycles.
- Every path ends in an `ESCALATE` or `RESOLVE` step. A failed step with no `on_failure` escalates.
- `RESOLVE` can only be reached straight from the success edge of a `VERIFY` step (only `NOTE` steps may sit in between).
- Adapter parameters must match the closed schema for that step type in [src/l1_agent/adapters/schemas.py](src/l1_agent/adapters/schemas.py).
- Every `${name}` placeholder must be a declared parameter.

Step types: `SPLUNK_SEARCH`, `MQ_CHECK`, `FILE_CHECK`, `AUTOSYS_STATUS`, `DYNATRACE_VM_CHECK`, `DYNATRACE_METRICS`, `WEB_UI_CHECK`, `MAINFRAME_CHECK`, `DECISION`, `NOTE`, `VERIFY`, `ESCALATE`, `RESOLVE`.

A parameter is bound from the ticket before any step runs:

```json
{
  "name": "job_name",
  "type": "string",
  "source": {"kind": "extraction", "fields": ["short_description", "description"],
             "regex": "\\b[Jj]ob\\s+([A-Z][A-Z0-9_.\\-]{2,63})\\b"},
  "pattern": "[A-Z][A-Z0-9_.\\-]{2,63}"
}
```

Sources are `ticket_field`, `extraction` and `constant`. A string parameter with no `pattern` is limited to letters, digits, `_`, `.` and `-`.

A verification runs a read check and asserts on its output:

```json
{
  "step_id": "step-verify",
  "step_type": "VERIFY",
  "parameters": {
    "check": "MQ_CHECK",
    "parameters": {"queue_manager": "${queue_manager}", "queue": "${queue}", "action": "depth"},
    "assert": "result.current_depth < 1000",
    "retries": 2,
    "wait_seconds": 30
  },
  "on_success": "step-resolve",
  "on_failure": "step-backlog"
}
```

`DECISION` rules and `VERIFY` assertions use a small expression language: comparisons, `in`, `and`, `or`, `not`, and paths such as `result.jobs[0].status` or `steps["step-2"].current_depth`. An expression that cannot be evaluated never passes.

A prose knowledge article fetched from ServiceNow becomes a draft. Drafts are never matched or executed. The review and approval workflow is Phase 3.

## API

Every route is denied unless it is declared with an access rule; the service will not start otherwise.

| Route | Needs | Purpose |
|---|---|---|
| `POST /webhook/incident` | HMAC signature | Incident intake |
| `GET /health` | nothing | Liveness only, no detail |
| `GET /api/me` | any sign-in | Who is signed in, roles, CSRF token |
| `GET /api/dashboard/summary`, `/api/incidents[/{number}]`, `/api/sops/stats` | `incidents.read` | Runs and their evidence |
| `GET /api/stream` | `incidents.read` | Server-sent events: `incident_received`, `sop_matched`, `step_executing`, `step_done`, `incident_resolved`, `incident_escalated`, `heartbeat` |
| `GET /api/status`, `/api/openapi.json` | `system.read` | Mode, metrics snapshot, API description |
| `GET /metrics` | `metrics.read` | Prometheus text format |
| `GET /api/audit/events`, `/api/audit/verify` | `audit.read` | The audit log and its integrity |
| `/api/tokens`, `/api/role-mappings` | `tokens.manage`, `roles.manage` | Access administration |
| `/auth/login`, `/auth/callback`, `/auth/saml/*`, `/auth/break-glass`, `/auth/logout` | public, except logout | Sign-in and sign-out |

A signed-in browser sends `X-CSRF-Token` (from `/api/me`) on every request that changes something. API tokens go in `Authorization: Bearer`.

## Tests

```bash
pytest tests/ -v                         # live provider tests are excluded by default
ruff check src/ tests/ && ruff format --check src/ tests/
```

Tests in `tests/db` and `tests/api` run on SQLite and, when a PostgreSQL is available, on PostgreSQL too. Row level security, row locking and the audit guard exist only there:

```bash
docker run -d --name l1-pg -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:5432:5432 postgres:16
export L1_TEST_PG_ADMIN_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres
pytest tests/ -v
```

Without that variable the PostgreSQL tests are skipped locally. CI sets it and fails if any is skipped.

What the key tests prove:

- [tests/unit/engine/test_no_unverified_resolve.py](tests/unit/engine/test_no_unverified_resolve.py): no route to `resolved` without verification, in the state machine, in every sample SOP and in random SOP graphs.
- [tests/db/test_runs_durable.py](tests/db/test_runs_durable.py) and [tests/db/test_worker_killed.py](tests/db/test_worker_killed.py): one run per incident under concurrent delivery, and a killed worker's run is resumed without repeating a completed step.
- [tests/db/test_tenant_isolation.py](tests/db/test_tenant_isolation.py): with the tenant filter deliberately left out, a query still sees one tenant.
- [tests/db/test_audit_log.py](tests/db/test_audit_log.py): a changed, removed, inserted or reordered audit event is detected.
- [tests/api/test_http_access.py](tests/api/test_http_access.py): an undeclared route stops start-up, and every route refuses a caller without the right credentials.

## Layout

```
src/l1_agent/
  main.py, cli.py      service assembly and the l1-agent command
  demo.py              sample incidents through the real engine
  engine/              state machine, executor, journal, expressions, parameters, matcher, processor
  service/runs.py      intake, job queue, worker, run records
  db/                  schema, tenant-scoped connections, migrations
  audit/               hash-chained audit log
  auth/                roles, sessions, API tokens, OpenID Connect, SAML, break-glass
  api/                 routes, access control, middleware
  sop/                 SOP validator
  adapters/            seven tool adapters, their mocks, and parameter schemas
  ai/                  model client and SOP ranking (advice only)
  clients/             ServiceNow client
  escalation/          L2 routing and the escalation flow
  models/ store/ notifications/ config/ utils/
frontend/              React + Vite dashboard
data/sample_sops/      sample SOPs
docs/                  target architecture, threat model, controls matrix, ADRs, delivery plan
```

[ARCHITECTURE.md](ARCHITECTURE.md) describes the code as built and [RUNBOOK.md](RUNBOOK.md) covers running it. [docs/](docs/) holds the target design and the plan to reach it.

## Known limits

- One deployment serves one tenant's ServiceNow, identity provider and tool connections. The data layer isolates tenants; the connections are not yet per tenant.
- Adapters are read only. There are no remediation actions, approvals or policy engine yet.
- SOPs are files or knowledge articles; there is no review and approval workflow yet.
- The audit log is not yet exported to a SIEM and has no retention control.
- Connector behaviour is tested against mocks and hand-written fixtures only, and sign-in against faked identity providers only.
