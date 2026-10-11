# L1 Engineer Agent

An async Python service that takes a ServiceNow incident, matches it to a published Standard Operating Procedure (SOP), runs the SOP's diagnostic steps through tool adapters, and ends in one explicit outcome: resolved with verification, or handed to a person with the evidence. A React dashboard shows what the agent did.

**Status: pre-release.** This is a working demo being turned into a product. It runs against mock adapters. Behaviour against a real ServiceNow, Autosys, Splunk, Dynatrace, IBM MQ or mainframe is unverified. It holds no security or compliance certification. The plan, architecture, threat model and controls matrix are in [docs/](docs/).

## What it does today

- Receives incidents by signed webhook or by polling ServiceNow.
- Matches each incident to an SOP by keyword, CI, category and assignment group. A model, if configured, can help choose between candidates that already clear the confidence threshold. It has no tools and cannot decide what runs.
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

The service refuses to start unless access control is configured.

```bash
cp .env.example .env     # then fill in values; never commit .env
pip install -r requirements.txt -r requirements-connectors.txt
python -m src.l1_agent.main
```

| Variable | Purpose |
|---|---|
| `AGENT_API_TOKENS` | Comma-separated bearer tokens for every route except the webhook and `/health`. At least 32 characters each. |
| `AGENT_WEBHOOK_SECRET` | Shared secret for the webhook signature. At least 32 characters. |
| `AGENT_WEBHOOK_HOST` | Bind address. Defaults to `127.0.0.1`. |
| `AGENT_CORS_ORIGINS` | Exact browser origins allowed to call the API. Empty by default; `*` is refused. |
| `L2_ROUTING_PATH` | Your CI-pattern to L2-team routing file. The packaged one is an example. |
| `SECRETS_BACKEND` | `env`, `vault` or `aws`. Fetched secrets are never written to the process environment. |

Webhook callers sign each request:

```
X-L1-Timestamp: <unix seconds>
X-L1-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>" with AGENT_WEBHOOK_SECRET>
```

Requests older than five minutes and repeated signatures are rejected.

This token scheme is a stopgap. Single sign-on, roles and tenant isolation are Phase 2 of the [delivery plan](docs/delivery-plan.md). Terminate TLS in front of the service; it does not serve HTTPS itself.

TLS verification towards Splunk, Dynatrace and IR360 is always on, and the mainframe connection uses TLS. The switches that turn these off are accepted only in demo mode.

For the dashboard, set `L1_API_TOKEN` in `frontend/.env` so the Vite dev proxy can add the bearer token on the server side. Branding is set with the `VITE_BRAND_*` variables in `frontend/.env.example`.

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

| Route | Auth | Purpose |
|---|---|---|
| `POST /webhook/incident` | HMAC signature | Incident intake |
| `GET /health` | none | Liveness only, no detail |
| `GET /api/status` | bearer | Metrics snapshot and mode |
| `GET /metrics` | bearer | Prometheus text format |
| `GET /api/dashboard/summary` | bearer | KPI counts and breakdowns |
| `GET /api/incidents[/{number}]` | bearer | Incident history and execution trace |
| `GET /api/sops/stats` | bearer | Per-SOP outcome counts |
| `GET /api/stream` | bearer | Server-sent events: `incident_received`, `sop_matched`, `step_executing`, `step_done`, `incident_resolved`, `incident_escalated`, `heartbeat` |

The `/sop-editor/` pages exist only in demo mode. They write SOP files with no review step, so they are not part of the product.

## Tests

```bash
pytest tests/ -v                         # live model tests are excluded by default
pytest tests/unit/ --cov=src/l1_agent
ruff check src/ tests/ && ruff format --check src/ tests/
```

[tests/unit/engine/test_no_unverified_resolve.py](tests/unit/engine/test_no_unverified_resolve.py) searches the run state machine and every sample SOP for a route to `resolved` without verification, and runs randomly generated SOP graphs against adapters that pass, fail and raise.

## Layout

```
src/l1_agent/
  main.py              service assembly and routes
  demo.py              sample incidents through the real engine
  engine/              state machine, executor, expressions, parameters, matcher, processor
  sop/                 SOP validator
  adapters/            seven tool adapters, their mocks, and parameter schemas
  ai/                  model client and SOP ranking (advice only)
  api/                 dashboard handlers, SSE, access control
  clients/             ServiceNow client
  escalation/          L2 routing and the escalation flow
  models/ store/ events/ notifications/ config/ utils/
frontend/              React + Vite dashboard
data/sample_sops/      sample SOPs
docs/                  target architecture, threat model, controls matrix, ADRs, delivery plan
```

[ARCHITECTURE.md](ARCHITECTURE.md) and [RUNBOOK.md](RUNBOOK.md) predate this phase and still describe removed execution modes in places. Each carries a notice at the top. Treat [docs/](docs/) as current.

## Known limits

- State is in memory and in JSONL files. A restart loses in-flight incidents and the duplicate-detection record. A database and durable queue are Phase 2.
- One tenant, one set of credentials.
- No single sign-on or roles; shared bearer tokens only.
- No audit log beyond operational logs and ServiceNow work notes.
- Connector behaviour is tested against mocks and hand-written fixtures only.
