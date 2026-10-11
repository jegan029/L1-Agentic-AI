# L1 Engineer Agent

An async Python service that takes a ServiceNow incident, matches it to a published Standard Operating Procedure (SOP), runs the SOP's steps through connectors, and ends in one explicit outcome: resolved with verification, or handed to a person with the evidence. Where the SOP and the tenant's policy allow it, it carries out the fix the SOP describes; by default it only records what it would have done. A React dashboard shows what the agent did.

**Status: pre-release.** This is a working demo being turned into a product. The ServiceNow connector has run against one developer instance (basic authentication; OAuth has not) and the model client against one hosted provider. Every operations connector (Autosys, Splunk, Dynatrace, IBM MQ, mainframe, file shares, browser) has only run against a mock and against fixtures written from vendor documentation, and sign-in has only run against identity providers faked in the tests. The force start has only run against a mock scheduler: the real `sendevent` command, its exit codes and its timing are unverified. Opening a standard change from a template, adding a note, asking a group for approval and closing the change have run against one developer instance. Reading an approval or a rejection that a person gave there has not, and nor has the whole action pipeline. It holds no security or compliance certification. The plan, architecture, threat model and controls matrix are in [docs/](docs/).

## What it does today

- Receives incidents by signed webhook or by polling ServiceNow, and claims each ticket before working it. A ticket someone else already holds is left alone.
- Keeps SOPs as versions with a review workflow: one person submits a draft, a different person approves it, and only a published version is ever matched or run. Articles pulled from a ServiceNow knowledge base or a Git repository arrive as drafts.
- Matches each incident to a published SOP by keyword, CI, category and assignment group. A field the ticket does not carry is left out of the score; it is not counted as a mismatch. A model, if configured, can help choose between candidates that already clear the confidence threshold. It has no tools and cannot decide what runs.
- Binds typed parameters from the ticket (for example an Autosys job name), validates them, and stops if any cannot be bound.
- Executes the SOP step by step through connectors: Splunk, IBM MQ (through IR360), Autosys, file shares, Dynatrace, a browser check and mainframe TN3270. Each connector declares its capabilities, their parameters and their risk tier in a manifest.
- Carries out one kind of change so far: an Autosys force start of a failed job. Every change goes through a policy decision, a ServiceNow change record, an approval where one is required, pre-checks, verification and, where the connector declares one, rollback. A new tenant starts in shadow mode, where nothing is changed.
- Ends every run in one of four outcomes.

| Outcome | Meaning |
|---|---|
| `resolved` | A VERIFY step ran and its assertion showed the symptom is gone. The incident is resolved in ServiceNow. |
| `escalated` | The SOP routed the incident to an L2 team, with the evidence collected. |
| `needs_human` | The agent could not continue safely: invalid SOP, unbound parameter, adapter unavailable, internal error. The incident stays open and is routed to a person. |
| `aborted` | Stopped on purpose: the ticket was already assigned to someone else, or the kill switch was on when the run reached a change. The incident is routed to a person. |

There is no default outcome. A run cannot be recorded as resolved without a passed verification; see [ADR 0002](docs/adr/0002-fail-closed-outcomes.md).

### Changes the agent can make

An SOP step of type `ACTION` names a write capability. There is one: `autosys.job.force_start`. See [ADR 0008](docs/adr/0008-actions-policy-and-approvals.md).

| Autonomy level | What happens at an `ACTION` step |
|---|---|
| `shadow` (default) | The agent records what it would have done and changes nothing. |
| `recommend` | It posts the proposed action to the ticket and escalates. |
| `approval` | It opens a change record, waits for a person to approve, then acts. |
| `automatic` | It acts without a person, for tier 1 actions the tenant has listed. Anything else waits for approval. |

- The level that applies is the lowest of three: the tenant's, the one set for the SOP version that is running, and the deployment's ceiling. A newly published SOP version starts in shadow.
- **Tier 2 actions always need an approval**, and the approver cannot be the author of the SOP.
- An approval is for one exact action. If the parameters change, or the request expires, it is void.
- **Kill switch.** An operator or tenant administrator can stop every change for the tenant at once; the platform operator can stop them for every tenant. Only a tenant administrator can release the tenant's switch.
- A connector refuses a write unless the policy engine has recorded a decision and issued a single-use grant for exactly that call.

## Quick start (demo mode)

Requires Python 3.11+ and Node 18+.

```bash
pip install -r requirements-dev.txt

# One-shot demo: eight sample incidents through the real engine, with mocks.
# Three of them restart a failed job: automatically, after an approval, and unsuccessfully.
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
- A check's parameters must match the closed schema its connector declares. `l1-agent connectors` lists every capability; the schemas are in [src/l1_agent/connectors/ops/](src/l1_agent/connectors/ops/).
- Every `${name}` placeholder must be a declared parameter.

Step types: `CHECK`, `DECISION`, `NOTE`, `VERIFY`, `ESCALATE`, `RESOLVE`. A `CHECK` step names a connector capability:

```json
{"step_id": "step-2", "step_type": "CHECK", "capability": "splunk.search",
 "parameters": {"query": "index=app_logs ERROR"}, "on_success": "step-3"}
```

The older typed step names still work and mean the same thing: `SPLUNK_SEARCH` (`splunk.search`), `MQ_CHECK` (`ibm_mq.queue.check`), `FILE_CHECK` (`fileshare.file.read`), `AUTOSYS_STATUS` (`autosys.job.status`), `DYNATRACE_VM_CHECK` (`dynatrace.host.health`), `DYNATRACE_METRICS` (`dynatrace.metrics.query`), `WEB_UI_CHECK` (`browser.page.check`), `MAINFRAME_CHECK` (`mainframe.screen.check`).

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

### From draft to published

```
draft ──submit──▶ in review ──approve──▶ approved ──publish──▶ published ──▶ superseded | retired
             ◀──changes requested──
```

- A draft comes from an author in the dashboard (**SOP library**), from `l1-agent sop import --file sop.json`, or from a knowledge source (`l1-agent knowledge sync`). A structured article keeps its steps; a prose article becomes a draft with no steps and the article text beside it.
- Submitting runs the validator and fixes the content hash. The reviewer sees the validator results, every connector capability the SOP calls with its risk tier, what changed from the published version, and the source article.
- Approval is of that hash, by someone other than the author. The service refuses the author and so does a database constraint. A machine token cannot author or approve, and there is no command-line way to approve or publish.
- A published version cannot be edited. Publishing a new version supersedes the old one; a run that had already started finishes on the version it started with. Every run records the SOP id, version number and hash.

In demo mode the sample SOPs are published at start-up, and one sample is left in review so the screen has something to approve. The demo user holds every role but still cannot approve its own drafts.

## API

Every route is denied unless it is declared with an access rule; the service will not start otherwise.

| Route | Needs | Purpose |
|---|---|---|
| `POST /webhook/incident` | HMAC signature | Incident intake |
| `GET /health` | nothing | Liveness only, no detail |
| `GET /api/me` | any sign-in | Who is signed in, roles, CSRF token |
| `GET /api/dashboard/summary`, `/api/incidents[/{number}]`, `/api/sops/stats` | `incidents.read` | Runs and their evidence |
| `GET /api/stream` | `incidents.read` | Server-sent events: `incident_received`, `sop_matched`, `step_executing`, `step_done`, `incident_resolved`, `incident_escalated`, `awaiting_approval`, `heartbeat` |
| `GET /api/status`, `/api/openapi.json` | `system.read` | Mode, metrics snapshot, API description |
| `GET /metrics` | `metrics.read` | Prometheus text format |
| `GET /api/audit/events`, `/api/audit/verify` | `audit.read` | The audit log and its integrity |
| `/api/tokens`, `/api/role-mappings` | `tokens.manage`, `roles.manage` | Access administration |
| `GET /api/sops`, `/api/sop-versions/{id}` | `incidents.read` | SOP versions; one version with its checks, capabilities and changes |
| `POST /api/sops`, `PUT`/`DELETE /api/sop-versions/{id}`, `POST .../submit`, `POST /api/knowledge/sync` | `sops.author` | Write, submit and discard drafts; pull drafts from the knowledge sources |
| `POST /api/sop-versions/{id}/approve`, `.../request-changes`, `.../publish`, `POST /api/sops/{sop_id}/retire` | `sops.approve` | Review. The author of a version cannot approve it |
| `GET /api/connectors` | `system.read` | Every connector's manifest and whether this tenant has configured it. No credentials |
| `PUT`/`DELETE /api/connectors/{id}/connection`, `POST /api/connectors/{id}/test` | `connectors.manage` | A connector's settings and credential references; test the connection |
| `GET /api/actions[/{id}]` | `incidents.read` | Every action: the policy decision, the approval, the result |
| `POST /api/actions/{id}/approve`, `.../reject` | `actions.approve` | Decide a waiting action. The request carries the hash of the action that was shown |
| `GET /api/policy` | `system.read` | Autonomy level, rules, kill switches, deployment ceiling |
| `PUT /api/policy`, `POST /api/kill-switch/release` | `policy.manage` | Change the level or the rules; release the kill switch |
| `POST /api/kill-switch/engage` | `actions.stop` | Stop every change for the tenant |
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
- [tests/db/test_sop_lifecycle.py](tests/db/test_sop_lifecycle.py): a draft, or anything not published, cannot reach the engine; an author cannot approve their own version, in the service or by writing to the table; a submitted version cannot be changed.
- [tests/unit/connectors/test_manifest_lint.py](tests/unit/connectors/test_manifest_lint.py): a connector whose write capability lacks a tier, verify operation, closed schema, scopes or allow list is not registered.
- [tests/unit/connectors/test_secrets.py](tests/unit/connectors/test_secrets.py): a credential rotated in the secrets backend is used on the next call without a restart.
- [tests/contract/](tests/contract/): each connector against recorded conversations, including errors and paging. The recordings were written by hand from vendor documentation; they are not captures from live systems.

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
  sop/                 SOP validator, version store and lifecycle, ingestion from knowledge sources
  connectors/          sdk/ (manifest, lint, registry, credential references, HTTP client),
                       itsm/ (ServiceNow), knowledge/ (ServiceNow KB, Git), ops/ (the seven),
                       hub.py (a tenant's connections)
  adapters/            the seven original adapters the ops connectors run through, and their mocks
  ai/                  model client and SOP ranking (advice only)
  escalation/          L2 routing and the escalation flow
  models/ store/ notifications/ config/ utils/
frontend/              React + Vite dashboard
data/sample_sops/      sample SOPs (published in demo mode); data/sample_drafts/ (left in review)
docs/                  target architecture, threat model, controls matrix, ADRs, delivery plan
```

[ARCHITECTURE.md](ARCHITECTURE.md) describes the code as built and [RUNBOOK.md](RUNBOOK.md) covers running it. [docs/](docs/) holds the target design and the plan to reach it.

## Known limits

- One deployment serves one tenant's ServiceNow and identity provider. Operations connectors and knowledge sources are per tenant.
- One write capability exists, the Autosys force start, and it has only run against a mock scheduler. The Autosys REST API is not implemented; the command line is.
- Opening a standard change from a template, adding a note, asking a group for approval and closing the change have run against one developer instance. Reading an approval or a rejection that a person gave there has not, and nor has the whole action pipeline against a real instance. An existing normal change on the ticket is not accepted as the change record, and ServiceNow approvals are polled, not pushed.
- Policy rules are edited as JSON through the API or the command line. The dashboard sets the autonomy level and the kill switch and shows the rules.
- A grant is issued and checked inside one process. Checking it in a separate runner needs the key split planned for the packaging phase.
- Knowledge sources are a ServiceNow knowledge base and Git. Confluence and SharePoint are not built. A prose article becomes an empty draft; nothing drafts the steps for you yet.
- Connections are set from the command line or the API; the dashboard has no screen for them yet.
- The audit log is not yet exported to a SIEM and has no retention control.
- Connector behaviour is tested against mocks and hand-written fixtures only, and sign-in against faked identity providers only. ServiceNow OAuth has not run against a real instance.
