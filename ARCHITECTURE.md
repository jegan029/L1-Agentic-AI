# L1 Virtual Engineer Agent — Architecture & Design Document

## 1. Overview

The L1 Virtual Engineer Agent automates L1 incident triage and resolution by:
1. Ingesting incidents from ServiceNow (webhook or polling)
2. Matching incidents to Standard Operating Procedures (SOPs)
3. Executing SOP steps using approved tool integrations
4. Updating the ServiceNow incident with evidence and outcomes
5. Escalating to L2 when appropriate

## 2. Component Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│                          L1 Agent Service                                 │
│                                                                           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐  │
│  │ Webhook      │  │ Polling      │  │ Health Check │  │ Prometheus  │  │
│  │ Listener     │  │ Worker       │  │ GET /health  │  │ GET /metrics│  │
│  │ POST /webhook│  │ (async loop) │  └──────────────┘  └─────────────┘  │
│  └──────┬───────┘  └──────┬───────┘                                      │
│         │                 │                                               │
│         └────────┬────────┘                                               │
│                  ▼                                                        │
│  ┌───────────────────────────────────┐                                    │
│  │        Incident Processor         │                                    │
│  │  - Idempotency check              │                                    │
│  │  - AI or rule-based routing       │                                    │
│  │  - SOP matching / execution       │                                    │
│  │  - Incident update                │                                    │
│  │  - Escalation handling            │                                    │
│  │  - Resolution memory recording    │                                    │
│  └───────────┬───────────────────────┘                                    │
│              │                                                            │
│      ┌───────┴──────┐                                                     │
│      ▼              ▼                                                     │
│  ┌──────────────────────────┐   ┌─────────────────────────────┐          │
│  │    AI / LLM Engine       │   │   Rule-Based Engine          │          │
│  │  ┌────────────────────┐  │   │  ┌──────────┐  ┌─────────┐  │          │
│  │  │ LLM Client         │  │   │  │ SOP      │  │ SOP     │  │          │
│  │  │ (OpenAI-compat.)   │  │   │  │ Matcher  │  │ Parser  │  │          │
│  │  └────────────────────┘  │   │  │ + Memory │  └─────────┘  │          │
│  │  ┌────────────────────┐  │   │  │  Boost   │               │          │
│  │  │ AI Analyzer        │  │   │  └──────────┘  ┌──────────┐ │          │
│  │  │ (SOP selection)    │  │   │                │ SOP      │ │          │
│  │  └────────────────────┘  │   │                │ Executor │ │          │
│  │  ┌────────────────────┐  │   │                └──────────┘ │          │
│  │  │ AI Executor        │  │   └─────────────────────────────┘          │
│  │  │ (tool-calling loop)│  │                                            │
│  │  └────────────────────┘  │                                            │
│  └──────────────────────────┘                                            │
│                                                                           │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │                        Tool Adapters                               │   │
│  │  ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌───────────────────────┐  │   │
│  │  │ Splunk  │ │  IR360  │ │ Autosys  │ │   Windows Share       │  │   │
│  │  │ REST API│ │ MQ API  │ │ CLI      │ │   SMB Reader          │  │   │
│  │  └─────────┘ └─────────┘ └──────────┘ └───────────────────────┘  │   │
│  │  ┌───────────┐ ┌──────────────┐ ┌─────────────────────────────┐  │   │
│  │  │ Dynatrace │ │ Web UI       │ │ Mainframe TN3270            │  │   │
│  │  │ REST API  │ │ Scraper      │ │ BEIM ASYNC                  │  │   │
│  │  └───────────┘ └──────────────┘ └─────────────────────────────┘  │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                           │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │                       New in v2                                    │   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────┐  │   │
│  │  │ SOP Authoring   │  │ Resolution       │  │ Secrets Vault    │  │   │
│  │  │ UI              │  │ Memory           │  │ Provider         │  │   │
│  │  │ GET /sop-editor │  │ (learning loop)  │  │ Vault / AWS SSM  │  │   │
│  │  └─────────────────┘  └─────────────────┘  └──────────────────┘  │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                           │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │                  New in v3 — Escalation Engine                     │   │
│  │  ┌─────────────────┐  ┌──────────────────┐  ┌─────────────────┐  │   │
│  │  │ L2Router        │  │ EmailNotifier     │  │ EventBus /      │  │   │
│  │  │ (CI→team        │  │ (SMTP/TLS         │  │ IncidentHistory │  │   │
│  │  │  fnmatch rules) │  │  best-effort)     │  │ Store (SSE+JSONL│  │   │
│  │  └─────────────────┘  └──────────────────┘  └─────────────────┘  │   │
│  │  ┌──────────────────────────────────────────────────────────────┐  │   │
│  │  │ escalate_to_l2(): route → SNOW update → email → history →SSE │  │   │
│  │  └──────────────────────────────────────────────────────────────┘  │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                           │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │                       Observability                                │   │
│  │  ┌──────────────┐  ┌────────────────┐  ┌──────────────────────┐  │   │
│  │  │ Structured   │  │ Metrics        │  │ Audit Trail          │  │   │
│  │  │ JSON Logging │  │ (Prometheus)   │  │ (per-step evidence)  │  │   │
│  │  └──────────────┘  └────────────────┘  └──────────────────────┘  │   │
│  └───────────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────────┘
```

## 3. Data Flow

### 3.1 Incident Lifecycle

```
1. INTAKE        ServiceNow incident arrives (webhook POST or polling query)
                 ↓
2. DEDUP         Check idempotency (have we processed this incident number?)
                 ↓
3. SOP MATCH     Match short_description + CI/category/assignment_group to SOP
                 → Memory-boosted scores applied (+/- 10% based on history)
                 → If no SOP or confidence < threshold → ESCALATE
                 ↓
4. EXECUTE       Parse SOP steps; execute sequentially via tool adapters
                 → Post work notes at each step
                 → Collect evidence (tool output summaries)
                 → Handle DECISION branching
                 → If step requires approval → ESCALATE
                 ↓
5. UPDATE        Post final work note with execution summary
                 Set incident state (Resolved or escalated)
                 ↓
6. COMPLETE      Log audit trail; update metrics; write resolution memory
```

### 3.2 AI-Driven Mode (Primary)

When `LLM_ENABLED=true`, the LLM is the primary decision-maker:

```
1. INTAKE        Incident arrives
                 ↓
2. AI ANALYSIS   LLM reads ticket (short_description, description, CI, category)
                 → Returns preliminary analysis and key observations
                 ↓
3. AI SOP MATCH  LLM selects the best SOP via function calling (select_sop tool)
                 → Returns SOP ID, confidence, and rationale
                 → If no match or low confidence → ESCALATE
                 ↓
4. AI EXECUTION  LLM drives a tool-calling loop:
                 → LLM decides which tool to call next (splunk_search, mq_check, etc.)
                 → Tool adapter executes the call, returns results
                 → LLM interprets results and decides next action
                 → Repeats until LLM calls resolve_incident or escalate_to_l2
                 ↓
5. UPDATE        Post conclusion work note with evidence summary
                 → Write outcome to ResolutionMemory if resolved
```

### 3.3 Rule-Based Mode (Fallback)

When `LLM_ENABLED=false` or the LLM is unreachable, the system falls back to
deterministic keyword/regex matching and sequential step execution.

### 3.4 Rule-Based SOP Matching Algorithm

The rule-based matcher scores each SOP against the incident using weighted criteria:

| Criterion | Weight | Method |
|-----------|--------|--------|
| Keyword match in short_description | 0.50 | Exact substring + regex patterns |
| CI (CMDB CI) match | 0.20 | Exact match against applicable_services |
| Category match | 0.15 | Exact match against applicable_categories |
| Assignment group match | 0.15 | Exact match against applicable_assignment_groups |

After base scoring, historical success rate (from ResolutionMemory) is applied:

| Historical Success Rate | Boost Factor |
|------------------------|--------------|
| ≥ 70 % | +up to 10 % |
| 30–70 % | neutral |
| ≤ 30 % | −up to 10 % |
| < 3 records | no adjustment (insufficient data) |

- Score >= threshold (default 0.6): proceed with SOP
- Score < threshold: escalate to L2 with explanation

### 3.5 Step Execution

Each SOP step is dispatched to the appropriate tool adapter:

| Step Type | Adapter | Parameters |
|-----------|---------|------------|
| `SPLUNK_SEARCH` | SplunkAdapter | query, time_range, index |
| `MQ_CHECK` | IR360Adapter | queue_manager, queue, action (depth/browse/status) |
| `FILE_CHECK` | WindowsShareAdapter | unc_path, pattern, last_n_lines, time_window |
| `AUTOSYS_STATUS` | AutosysAdapter | job_name, action (status/dependencies) |
| `DYNATRACE_VM_CHECK` | DynatraceAdapter | host_name, host_group |
| `DYNATRACE_METRICS` | DynatraceAdapter | metric_selector, entity_selector, time_range |
| `WEB_UI_CHECK` | WebUIScraperAdapter | url, check_type, click_steps, expected_text |
| `MAINFRAME_CHECK` | MainframeAdapter | job_name, expected_status, action |
| `DECISION` | Internal | rule (any_failed, all_success, custom) |
| `NOTE` | Internal | text (posted as work note) |

## 4. Security Model

### 4.1 Principles

- **Read-only by default**: All tool adapters perform read-only operations
- **Approval gates**: Steps with `requires_approval: true` trigger escalation
- **Least privilege**: Each adapter uses minimum required permissions
- **No secrets in code**: All credentials via vault/SSM or environment variables

### 4.2 Input Validation

| Component | Validation |
|-----------|------------|
| Autosys CLI | Job name regex: `^[a-zA-Z0-9_.\-/]+$` (no shell metacharacters) |
| Windows share | UNC paths validated against allow-list prefixes |
| Splunk queries | Parameterized via REST API (no shell execution) |
| IR360 | API calls with typed parameters; actions validated against allowlist |
| SOP Editor | SOP IDs validated with `^[A-Za-z0-9_\-]+$`; JSON parsed before save |

### 4.3 Log Redaction

Sensitive fields are automatically redacted in structured logs:
- `password`, `token`, `secret`, `api_key`, `authorization`

### 4.4 RBAC

| Role | Permissions |
|------|-------------|
| Agent (service account) | Read incidents, read SOPs, post work notes, read-only tool access |
| L2 Engineer | Approve write actions, override escalations |
| Admin | Configure SOPs via SOP Editor, manage allow-lists, view audit logs |

## 5. Reliability

### 5.1 Retry Strategy

- Exponential backoff: `base_delay * 2^attempt` with jitter
- Default: 3 attempts, 1s base delay
- Configurable per adapter

### 5.2 Circuit Breaker

Each tool adapter has an independent circuit breaker:
- **Closed** (normal): requests pass through
- **Open** (tripped): requests fail immediately (after N consecutive failures)
- **Half-open**: after reset timeout, allow one probe request

Default: 5 failures to trip, 60s reset timeout.

### 5.3 Concurrency

- `asyncio.Semaphore` limits concurrent incident processing (default: 5)
- Each incident processed in its own async task
- Polling and webhook intake run concurrently

### 5.4 Idempotency

- Incident numbers tracked in a processed set
- Re-processing the same incident is a no-op
- Work notes include timestamps to detect duplicates

## 6. Observability

### 6.1 Structured Logging

All logs are JSON-lines format with:
- `timestamp` (ISO 8601)
- `level` (INFO, WARNING, ERROR)
- `correlation_id` (incident number)
- `component` (module name)
- `message`
- `extra` (context-specific fields)

### 6.2 Metrics

In-process metrics collector (`utils/metrics.py`) tracks counters, histograms,
and gauges, and exposes them via two endpoints:

| Endpoint | Format | Use |
|----------|--------|-----|
| `GET /health` | JSON | Readiness probes, quick dashboard |
| `GET /metrics` | Prometheus text | Grafana / Prometheus scrape target |

Key metrics:
- `l1_agent_incidents_received` (counter)
- `l1_agent_incidents_resolved` (counter)
- `l1_agent_incidents_escalated{reason=...}` (counter)
- `l1_agent_ir360_api_calls` (counter)
- `l1_agent_uptime_seconds` (gauge)

### 6.3 Audit Trail

Every step execution produces an audit record:
```json
{
  "step_id": "step-2",
  "step_type": "MQ_CHECK",
  "status": "success",
  "tool_called": "IR360",
  "input_summary": "queue_manager=QM1, queue=PAYMENT.IN",
  "output_summary": "depth=1523, status=RUNNING",
  "evidence_snippet": "Queue depth: 1523 (threshold: 1000)",
  "started_at": "2026-01-15T10:30:00Z",
  "completed_at": "2026-01-15T10:30:02Z",
  "duration_ms": 2100.0
}
```

## 7. Escalation Design

### 7.1 Escalation Triggers

| Trigger | Action |
|---------|--------|
| No matching SOP | Post "No SOP found" note, route to L2 via CI mapping |
| SOP confidence < threshold (or None) | Post alternatives, route to L2 via CI mapping |
| Tool access denied | Post error details, route to L2 via CI mapping |
| Ambiguous/inconsistent data | Post findings, route to L2 via CI mapping |
| Write action required | Post approval request, pause execution |
| Step execution failure (after retries) | Post error + evidence, route to L2 via CI mapping |

### 7.2 Central Escalation Function

All escalation paths funnel through a single `escalate_to_l2()` coroutine
(`src/l1_agent/escalation/escalator.py`) that performs these steps in order:

```
1. ROUTE    L2Router.resolve_l2_team(incident.cmdb_ci)
            → looks up config/l2_routing.json (fnmatch wildcard patterns)
            → returns {"name": "L2-Database", "email": "l2-db@example.com"}

2. SNOW     Update incident assignment_group to the resolved L2 team name

3. EMAIL    send_escalation_email(team_email, subject, body)
            → SMTP/TLS using SMTP_HOST/PORT/USER/PASSWORD/FROM env vars
            → best-effort: logs warning if unconfigured, escalation continues

4. HISTORY  history_store.record(incident, summary)   [if not already recorded]

5. SSE      event_bus.publish({
              "type":           "incident_escalated",
              "incident_number": "INC0012345",
              "reason":          "low_confidence",
              "l2_team_name":    "L2-Database",
              "l2_team_email":   "l2-db@example.com",
              "email_sent":       true
            })
```

### 7.3 CI-Based L2 Team Routing

`L2Router` (`src/l1_agent/escalation/l2_router.py`) loads routing rules from
`src/l1_agent/config/l2_routing.json` at startup. CI patterns use Python's
`fnmatch` (case-insensitive):

```json
{
  "default_team": { "name": "L2-General", "email": "l2-general@statestreet.com" },
  "ci_mappings": {
    "DB*":      { "name": "L2-Database",     "email": "l2-database@statestreet.com" },
    "MQ*":      { "name": "L2-Middleware",   "email": "l2-middleware@statestreet.com" },
    "APP*":     { "name": "L2-AppSupport",   "email": "l2-appsupport@statestreet.com" },
    "MF*":      { "name": "L2-Mainframe",    "email": "l2-mainframe@statestreet.com" },
    "AUTOSYS*": { "name": "L2-BatchScheduling", "email": "l2-batch@statestreet.com" }
  }
}
```

If no pattern matches or the CI is empty, the `default_team` is used.
The routing file can be updated without restarting the service; reload by
restarting the process or adding a hot-reload endpoint.

### 7.4 Escalation Packet

When escalating after partial SOP execution, the agent posts a structured summary:
```
[L1 Agent Escalation]
Incident: INC0012345
SOP: SOP-MQ-001 (MQ Queue Depth High)
Reason: Step failed after 3 retries

Checks performed:
  [OK] step-1: MQ queue depth check - depth=1523
  [OK] step-2: Splunk log search - 3 errors found
  [FAIL] step-3: Autosys job status - Connection timeout

Action needed: Verify Autosys connectivity; review job BATCH_PAYMENT_001
Routed to: L2-Middleware (l2-middleware@statestreet.com)
```

## 8. SOP Schema

### 8.1 SOP Definition

```json
{
  "sop_id": "SOP-MQ-001",
  "title": "MQ Queue Depth High - Investigation",
  "applicable_services": ["PaymentService", "OrderService"],
  "applicable_categories": ["Middleware"],
  "applicable_assignment_groups": ["L1-Middleware-Support"],
  "keywords": ["queue depth", "mq.*high", "message.*backlog"],
  "steps": [...],
  "pre_checks": ["Verify MQ monitoring access"],
  "tools_required": ["IR360", "Splunk"],
  "escalation_criteria": {
    "max_retry_count": 3,
    "escalate_on_access_denied": true,
    "escalate_on_ambiguous_result": true,
    "escalate_on_write_action": true
  },
  "version": "1.2"
}
```

### 8.2 Step Definition

```json
{
  "step_id": "step-2",
  "step_type": "MQ_CHECK",
  "description": "Check current queue depth",
  "parameters": {
    "queue_manager": "PROD.QM1",
    "queue": "PAYMENT.IN",
    "action": "depth"
  },
  "expected_output": "depth < 1000",
  "on_success": "step-3",
  "on_failure": "step-escalate",
  "requires_approval": false,
  "timeout_seconds": 30
}
```

## 9. Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| Async framework | asyncio + aiohttp |
| Data validation | Pydantic v2 |
| HTTP client | aiohttp.ClientSession |
| Testing | pytest + pytest-asyncio |
| Linting | ruff |
| Security scan | bandit |
| Container | Docker (python:3.11-slim) |
| CI/CD | GitHub Actions |
| Configuration | Environment variables (.env) |
| Secrets | HashiCorp Vault / AWS SSM (optional) |

## 10. AI / LLM Integration

### 10.1 LLM Client

The `LLMClient` (`src/l1_agent/ai/llm_client.py`) connects to any OpenAI-compatible
API endpoint. It supports:

- Chat completions with tool/function calling
- Configurable model, temperature, max tokens, timeout
- Automatic retry with exponential backoff
- API key loaded from vault/SSM or `LLM_API_KEY` environment variable

### 10.2 Tool Definitions

`src/l1_agent/ai/tool_definitions.py` exposes adapters as LLM-callable functions:

| Function | Adapter | Purpose |
|----------|---------|--------|
| `splunk_search` | SplunkAdapter | Search logs by query and time range |
| `mq_check` | IR360Adapter | Check MQ queue depth / status |
| `file_check` | WindowsShareAdapter | Read log file lines, filter by pattern |
| `autosys_status` | AutosysAdapter | Query job status or dependencies |
| `dynatrace_vm_check` | DynatraceAdapter | Check VM/host health |
| `dynatrace_metrics_check` | DynatraceAdapter | Query CPU/memory metrics |
| `web_ui_check` | WebUIScraperAdapter | Navigate URL click-path via headless Selenium |
| `mainframe_async_check` | MainframeAdapter | Check MF BEIM ASYNC job status via TN3270 |
| `post_work_note` | (internal) | Post a work note to the incident |
| `escalate_to_l2` | (internal) | Escalate with reason and findings |
| `resolve_incident` | (internal) | Resolve with summary and evidence |

### 10.3 AI Analyzer

`AIAnalyzer` sends the incident context to the LLM and asks it to:
1. Produce a preliminary analysis (key observations, likely root cause)
2. Select the best SOP via `select_sop` tool call (returns SOP ID + confidence + rationale)
3. Interpret individual tool results when needed

### 10.4 AI Executor

`AIExecutor` runs an async tool-calling loop:
1. Build a system prompt with SOP context, available tools, and investigation guidelines
2. Send to LLM; receive response
3. If response contains `tool_calls`, execute each via the appropriate adapter
4. Feed tool results back as `tool` messages
5. Repeat until the LLM calls `resolve_incident` or `escalate_to_l2` (or max iterations reached)

### 10.5 Mock LLM

`MockLLMClient` provides realistic responses without a real API. It supports:
- **Scripted mode**: Pre-defined responses returned in order (for deterministic tests)
- **Auto mode**: Generates contextual responses based on message content (for demos)

## 11. Adapter Reference

### 11.1 IR360 Adapter (`IR360Adapter`)

Connects to the IR360 REST API or local CLI to check IBM MQ queue managers.

**API mode** (default): Uses `aiohttp.ClientSession` with connection reuse, 30 s timeout,
and `X-API-Key` header authentication. Validates action against an allowlist
(`depth`, `browse`, `status`) before making any HTTP call.

**CLI mode** (`IR360_USE_CLI=true`): Wraps the CLI binary with `asyncio.create_subprocess_exec`
and enforces a 30 s timeout.

Configuration: `IR360_BASE_URL`, `IR360_API_KEY`, `IR360_CLI_PATH`, `IR360_USE_CLI`.

### 11.2 Dynatrace Adapter (`DynatraceAdapter`)

- **VM Health Check**: Entities API + Problems API
- **Metrics Check**: Metrics query API for CPU/memory time-series

### 11.3 Web UI Scraper Adapter (`WebUIScraperAdapter`)

Uses headless Selenium/ChromeDriver for automated click-path navigation.
Supports `page_load` and `click_path` check types.

### 11.4 Mainframe Adapter (`MainframeAdapter`)

TN3270 terminal emulation via py3270. All operations read-only.
Parses BEIM ASYNC job statuses: `inact ok`, `active`, `inact error`, etc.

## 12. New Features (v2)

### 12.1 Prometheus Metrics Endpoint

`GET /metrics` returns all in-process metrics in Prometheus text exposition format.
Scrape it from Prometheus with:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: l1-agent
    static_configs:
      - targets: ['l1-agent:8080']
    metrics_path: /metrics
```

Grafana datasource: add Prometheus pointing at your Prometheus server, then import
dashboards querying `l1_agent_*` metrics.

### 12.2 SOP Authoring UI

A lightweight web UI for creating and editing SOPs without hand-editing JSON files.

| Route | Description |
|-------|-------------|
| `GET /sop-editor/` | List all SOPs in the library |
| `GET /sop-editor/new` | Blank SOP creation form |
| `GET /sop-editor/{sop_id}` | Edit an existing SOP |
| `POST /sop-editor/save` | Save (create or update) SOP to disk |
| `POST /sop-editor/{sop_id}/delete` | Delete a SOP file |

SOPs are stored as JSON files in `data/sample_sops/`. The SOP Editor validates
the JSON before saving and enforces safe SOP IDs (`^[A-Za-z0-9_\-]+$`).

### 12.3 Resolution Memory & Learning Loop

`ResolutionMemory` (`src/l1_agent/engine/resolution_memory.py`) persists
outcome data for each resolved incident to a JSON-lines file
(`data/resolution_memory.jsonl`).

**Learning loop mechanism:**
1. When an incident resolves, a record is appended to the memory file.
2. On next startup, all records are loaded back into memory.
3. `SOPMatcher` applies a ±10 % boost/penalty to each SOP's score based on its
   historical success rate (requires ≥ 3 data points to adjust).
4. The `/health` endpoint exposes per-SOP stats (success rate, avg confidence,
   boost factor).

**Memory record schema:**
```json
{
  "incident_number": "INC0001234",
  "sop_id": "SOP-MQ-001",
  "outcome": "resolved",
  "confidence_used": 0.82,
  "short_description": "MQ queue depth high on QMPROD01",
  "category": "Middleware",
  "cmdb_ci": "PaymentService",
  "duration_ms": 4200.0,
  "resolved_at": "2026-04-18T10:00:00+00:00"
}
```

### 12.4 Secrets Vault Integration

`SecretsProvider` (`src/l1_agent/utils/secrets.py`) fetches sensitive credentials
at startup and injects them into `os.environ` before `Settings.from_env()` runs.
The rest of the codebase is unchanged regardless of which backend is used.

Select the backend with `SECRETS_BACKEND`:

| Value | Backend | Required packages |
|-------|---------|-------------------|
| `env` (default) | Read from environment variables | none |
| `vault` | HashiCorp Vault KV v2 | `pip install hvac` |
| `aws` | AWS SSM Parameter Store | `pip install boto3` |

**HashiCorp Vault configuration:**

| Variable | Description |
|----------|-------------|
| `VAULT_ADDR` | Vault server URL (e.g. `https://vault.internal:8200`) |
| `VAULT_TOKEN` | Token auth (recommended for CI) |
| `VAULT_ROLE_ID` / `VAULT_SECRET_ID` | AppRole auth (recommended for prod) |
| `VAULT_MOUNT` | KV v2 mount (default: `secret`) |
| `VAULT_PATH_PREFIX` | Path prefix inside the mount (default: `l1-agent`) |

**AWS SSM configuration:**

| Variable | Description |
|----------|-------------|
| `AWS_REGION` | AWS region (default: `us-east-1`) |
| `AWS_SSM_PREFIX` | Parameter path prefix (default: `/l1-agent`) |

Secrets are mapped to environment variables via `_SECRET_MAPPINGS` in
`utils/secrets.py`. Add entries there when introducing new integrations.

### 12.5 GitHub Actions CI/CD Pipeline

`.github/workflows/ci.yml` runs on every push and pull request to `main`/`develop`:

| Job | Trigger | What it does |
|-----|---------|--------------|
| `lint` | always | `ruff format --check` + `ruff check` |
| `unit-tests` | after lint | `pytest tests/unit/` with coverage ≥ 70 % |
| `integration-tests` | after unit tests | `pytest tests/integration/` (mock adapters) |
| `docker-build` | after unit tests | Build prod + demo images, smoke test |
| `security-scan` | after lint | Bandit static analysis; fails on HIGH severity |

Coverage reports are uploaded to Codecov. Bandit reports are stored as artifacts.

## 13. HTTP API Reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/incident` | Receive ServiceNow webhook (JSON body) |
| `GET` | `/health` | Service health + metrics snapshot + memory stats |
| `GET` | `/metrics` | Prometheus text format metrics |
| `GET` | `/sop-editor/` | SOP library list |
| `GET` | `/sop-editor/new` | New SOP form |
| `GET` | `/sop-editor/{id}` | Edit SOP form |
| `POST` | `/sop-editor/save` | Save SOP |
| `POST` | `/sop-editor/{id}/delete` | Delete SOP |
| `GET` | `/api/dashboard/summary` | KPIs, outcome breakdown, escalation reasons, 7-day trend |
| `GET` | `/api/incidents` | Paginated history (`?page=&per_page=&search=&outcome=&priority=`) |
| `GET` | `/api/incidents/{n}` | Single incident with full step trace |
| `GET` | `/api/sops/stats` | Per-SOP resolution statistics |
| `GET` | `/api/stream` | SSE stream of real-time agent events |

## 14. Configuration Reference

All settings are environment-variable-driven. Copy `.env.example` and set values.

### Core Agent

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_DEMO_MODE` | `false` | Use mock adapters and mock LLM |
| `LLM_ENABLED` | `true` | Enable AI-driven mode |
| `AGENT_SOP_CONFIDENCE_THRESHOLD` | `0.6` | Minimum score to proceed with SOP |
| `AGENT_MAX_CONCURRENT_INCIDENTS` | `5` | Semaphore limit |
| `AGENT_WEBHOOK_PORT` | `8080` | HTTP server port |
| `AGENT_LOG_LEVEL` | `INFO` | Logging level |

### Email (Escalation Notifications)

| Variable | Default | Description |
|----------|---------|-------------|
| `SMTP_HOST` | — | SMTP server hostname (leave blank to skip email) |
| `SMTP_PORT` | `587` | SMTP port (STARTTLS) |
| `SMTP_USER` | — | SMTP login username |
| `SMTP_PASSWORD` | — | SMTP login password |
| `SMTP_FROM` | (`SMTP_USER`) | Sender address for escalation emails |

### Secrets Backend

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRETS_BACKEND` | `env` | `env`, `vault`, or `aws` |
| `VAULT_ADDR` | `http://127.0.0.1:8200` | Vault server address |
| `VAULT_TOKEN` | | Vault token (CI/dev) |
| `VAULT_ROLE_ID` / `VAULT_SECRET_ID` | | AppRole credentials (prod) |
| `AWS_REGION` | `us-east-1` | AWS region for SSM |
| `AWS_SSM_PREFIX` | `/l1-agent` | SSM parameter path prefix |

## 15. New Features (v3)

### 15.1 Intelligent Escalation Engine

Three new modules implement a fully auditable L2 escalation pipeline:

| Module | Path | Purpose |
|--------|------|---------|
| `L2Router` | `escalation/l2_router.py` | Resolves CI → L2 team via fnmatch wildcard rules |
| `escalate_to_l2()` | `escalation/escalator.py` | Central coroutine: route, SNOW update, email, history, SSE |
| `send_escalation_email()` | `notifications/email_notifier.py` | SMTP/TLS email, best-effort |
| L2 routing config | `config/l2_routing.json` | CI pattern → team name + email (editable without code change) |

**Confidence threshold enforcement** — both AI and rule-based paths now explicitly guard against `None` confidence scores in addition to below-threshold values. The check is `if confidence is None or confidence < threshold`.

### 15.2 Frontend Dashboard (v3)

A React + Vite customer-facing dashboard at `frontend/`:

| Page | Data Source |
|------|------------|
| Live Dashboard | `GET /api/dashboard/summary` (polls every 10s) |
| Incident Feed | `GET /api/incidents` with pagination + filters |
| Incident Detail | `GET /api/incidents/{n}` (step-by-step trace) |
| SOP Performance | `GET /api/sops/stats` (polls every 30s) |
| Activity Feed | `GET /api/stream` (SSE, auto-reconnects) |

The `incident_escalated` SSE event now carries `l2_team_name`, `l2_team_email`, and `email_sent` so the Activity Feed can display the routed team in real time.

### 15.3 Bug Fixes

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| Backend hangs on restart when history exists | `ResolutionMemory.stats()` held `Lock` then called `success_rate()` / `average_confidence()` which re-acquired the same non-re-entrant `Lock` → deadlock | Compute all values inline within the single `with self._lock` block |
| Demo SOPs not found when cwd differs | `_load_demo_sops()` used `Path("data/sample_sops")` relative to cwd | Changed to `Path(__file__).resolve().parent.parent.parent / "data" / "sample_sops"` with cwd fallback |

### 15.4 Unit Tests

| Test File | Tests | Coverage |
|-----------|-------|---------|
| `tests/unit/test_l2_router.py` | 11 | Wildcard match, case-insensitive, empty CI, missing config, copy safety |
| `tests/unit/test_confidence_threshold.py` | 5 | None escalates, below threshold escalates, at threshold executes, above executes, no SOP escalates |

Run with: `pytest tests/unit/ -v`

## 16. Future Enhancements

- **Kubernetes deployment**: Helm chart with HPA for auto-scaling
- **Fine-tuned SOP matching model**: Train on historical incident-SOP pairs from ResolutionMemory
- **Slack/Teams notifications**: Alert on-call on L2 escalation with incident summary
- **Approval workflow UI**: Web dashboard for L2 engineers to approve write actions
- **Event-driven intake**: Kafka/RabbitMQ consumer for high-throughput environments
- **Multi-turn conversation**: Allow L2 engineers to chat with the agent about an incident
- **RAG over SOP corpus**: Vector search over large SOP libraries
- **Multi-tenant SOPs**: Scope SOPs by team so multiple teams share one agent instance
