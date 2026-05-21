# State Street L1 Engineer Agent — Operational Runbook

## 1. Deployment

### 1.1 Prerequisites

- Python 3.11+
- Node.js 18+ (frontend dashboard)
- Network access to: ServiceNow, Splunk REST API, IR360 API, Autosys CLI host, Windows file shares, Dynatrace API, mainframe TN3270 hosts
- Service account credentials for each integration

### 1.2 Container Deployment

```bash
# Build the image
docker build -t l1-agent:latest .

# Run with environment file
docker run -d \
  --name l1-agent \
  --env-file .env \
  -p 8080:8080 \
  l1-agent:latest

# Demo image (no credentials needed)
docker build --target demo -t l1-agent-demo:latest .
docker run --rm -p 8080:8080 l1-agent-demo:latest
```

### 1.3 Direct Deployment

```bash
# Install backend dependencies
cd L1_Agentic_AI_Solution
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with real credentials

# Start the backend service
python -m src.l1_agent.main
```

### 1.4 Frontend Dashboard Deployment

```bash
# Install frontend dependencies
cd frontend
npm install

# Development (with hot reload, proxies /api/* to localhost:8080)
npm run dev
# Opens at http://localhost:5173

# Production build
npm run build
# Outputs to frontend/dist/

# Serve production build via the backend (optional)
# Add to main.py: app.router.add_static('/dashboard', 'frontend/dist/', show_index=True)
# Then access at http://localhost:8080/dashboard
```

### 1.5 Running Both Services (Demo Mode)

Open three terminals:

```bash
# Terminal 1 — Backend (rule-based, no credentials needed)
cd L1_Agentic_AI_Solution
export AGENT_DEMO_MODE=true
export LLM_ENABLED=false
python -m src.l1_agent.main

# Terminal 2 — Frontend
cd frontend
npm run dev

# Terminal 3 — Fire test incidents (optional)
cd L1_Agentic_AI_Solution
python -m src.l1_agent.demo
```

Open **http://localhost:5173** in a browser to view the dashboard.

### 1.6 Running with CrewAI Pipeline

Requires an Anthropic API key. All other services (frontend, history store, SSE) work identically.

```bash
# Terminal 1 — Backend in CrewAI mode
cd L1_Agentic_AI_Solution
export AGENT_DEMO_MODE=true
export LLM_ENABLED=false
export CREWAI_ENABLED=true
export CREWAI_MODEL=claude-sonnet-4-6       # or another Anthropic model ID
export ANTHROPIC_API_KEY=sk-ant-...
export CREWAI_VERBOSE=true                  # optional: prints agent deliberation
python -m src.l1_agent.main

# Terminal 2 — Frontend (unchanged)
cd frontend
npm run dev
```

The four agents (`TriageAgent → ReviewAgent → ResolutionAgent → ResolverAgent`) run sequentially inside a thread-pool executor so the async event loop is never blocked. Watch their deliberation in the Activity Feed at `http://localhost:5173`.

### 1.6 Health Check

```
GET http://localhost:8080/health
```

Response:
```json
{
  "status": "healthy",
  "metrics": {
    "counters": { "incidents.received": 8, "incidents.resolved": 5 },
    "histograms": { "sop.execution_duration_ms": { "mean": 3820.4 } },
    "gauges": { "l1_agent_uptime_seconds": 3600 }
  },
  "memory": {}
}
```

---

## 2. Configuration

### 2.1 Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `SERVICENOW_BASE_URL` | ServiceNow instance URL | `https://company.service-now.com` |
| `SERVICENOW_USERNAME` | API username | `l1_agent_svc` |
| `SERVICENOW_PASSWORD` | API password | (from vault) |
| `SERVICENOW_ASSIGNMENT_GROUP` | Incident queue to monitor | `L1-Support` |

### 2.2 LLM / AI Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_ENABLED` | `false` | Enable AI/LLM path (AIAnalyzer + AIExecutor) |
| `LLM_ENDPOINT` | `https://api.openai.com/v1` | OpenAI-compatible API base URL |
| `LLM_API_KEY` | — | LLM API key — **store in secrets vault, never in code or logs** |
| `LLM_MODEL` | `gpt-4` | Model name (e.g. `gpt-4o`, `gpt-4`, `gpt-3.5-turbo`) |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature (lower = more deterministic) |
| `LLM_MAX_TOKENS` | `4096` | Maximum tokens per LLM response |
| `LLM_TIMEOUT_SECONDS` | `60` | HTTP timeout for LLM API calls |

### 2.3 CrewAI Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CREWAI_ENABLED` | `false` | Enable CrewAI 4-agent pipeline; takes precedence over `LLM_ENABLED` |
| `CREWAI_MODEL` | `claude-sonnet-4-6` | Anthropic model ID used by all four agents |
| `ANTHROPIC_API_KEY` | — | Anthropic API key — also checked as `CREWAI_API_KEY` |
| `CREWAI_VERBOSE` | `false` | Print agent deliberation to stdout (useful for debugging) |
| `CREWAI_MAX_ITER` | `15` | Maximum tool-call iterations per agent before forced stop |

### 2.4 Agent Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_DEMO_MODE` | `false` | Use mock adapters + pre-load sample SOPs from `data/sample_sops/` |
| `AGENT_LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `AGENT_WEBHOOK_PORT` | `8080` | Backend HTTP listening port |
| `AGENT_WEBHOOK_HOST` | `0.0.0.0` | Backend bind address |
| `AGENT_SOP_CONFIDENCE_THRESHOLD` | `0.6` | Minimum match confidence to execute a SOP (0–1) |
| `AGENT_MAX_CONCURRENT_INCIDENTS` | `5` | Semaphore limit for concurrent incident processing |
| `SERVICENOW_POLL_INTERVAL` | `30` | Seconds between ServiceNow polling cycles |

### 2.5 Tool Adapter Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SPLUNK_BASE_URL` | — | Splunk REST API base URL |
| `SPLUNK_TOKEN` | — | Splunk bearer token |
| `IR360_BASE_URL` | — | IR360 API base URL |
| `IR360_API_KEY` | — | IR360 API key |
| `AUTOSYS_CLI_PATH` | `autorep` | Path to autorep binary |
| `WINDOWS_SHARE_ALLOWED_PREFIXES` | — | Comma-separated UNC path prefixes |
| `DYNATRACE_BASE_URL` | — | Dynatrace environment URL |
| `DYNATRACE_API_TOKEN` | — | Dynatrace API token (scopes: `entities.read`, `metrics.read`, `problems.read`) |
| `MAINFRAME_HOST` | — | Mainframe TN3270 hostname |
| `MAINFRAME_PORT` | `23` | Mainframe TN3270 port |
| `WEBUI_ALLOWED_DOMAINS` | — | Comma-separated allowed domains for Selenium |

---

## 3. Dashboard API Reference

All endpoints are served on the same port as the backend (`8080` by default). The frontend Vite dev server proxies `/api/*` to the backend automatically.

### 3.1 Existing Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/incident` | Receive a ServiceNow incident webhook |
| `GET` | `/health` | Service health + full metrics snapshot |
| `GET` | `/metrics` | Prometheus-compatible text format for Grafana |

### 3.2 Dashboard Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/dashboard/summary` | KPIs, outcome breakdown, escalation reasons, 7-day trend |
| `GET` | `/api/incidents` | Paginated incident history |
| `GET` | `/api/incidents/{incident_number}` | Full execution trace for one incident |
| `GET` | `/api/sops/stats` | Per-SOP aggregated resolution stats |
| `GET` | `/api/stream` | SSE stream of real-time agent events |

#### `/api/incidents` Query Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `page` | int | Page number (default: 1) |
| `per_page` | int | Results per page (default: 20, max: 100) |
| `search` | string | Full-text search on incident number, description, SOP title |
| `outcome` | string | Filter: `resolved`, `escalated`, `failed`, `partial` |
| `priority` | int | Filter: `1`–`5` |

#### `/api/stream` SSE Event Types

| Event | When | Key Payload Fields |
|-------|------|--------------------|
| `incident_received` | Incident intake | `incident_number`, `short_description`, `priority` |
| `sop_matched` | After SOP selection | `incident_number`, `sop_id`, `sop_title`, `confidence` |
| `step_executing` | Before each step | `incident_number`, `step_id`, `step_type`, `description` |
| `step_done` | After each step | `incident_number`, `step_id`, `status`, `output_summary`, `duration_ms` |
| `incident_resolved` | Successful resolution | `incident_number`, `sop_id`, `duration_ms` |
| `incident_escalated` | L2 escalation | `incident_number`, `reason` |
| `heartbeat` | Every 25s | `ts` (keeps browser connection alive) |

### 3.3 Firing Test Incidents

**Incident that resolves via SOP (matches SOP-MQ-001):**
```bash
curl -X POST http://localhost:8080/webhook/incident \
  -H "Content-Type: application/json" \
  -d '{
    "sys_id": "test-resolve-001",
    "number": "INC0009001",
    "short_description": "MQ queue depth high on PAYMENT.REQUEST - messages not being consumed",
    "description": "PAYMENT.REQUEST queue on QMPROD01 building up since 09:00. Consumer PaymentService not processing.",
    "category": "Middleware",
    "subcategory": "MQ",
    "cmdb_ci": "PaymentService",
    "assignment_group": "L1-Middleware-Support",
    "priority": "2",
    "state": "1"
  }'
```

**Incident that escalates to L2 (no matching SOP):**
```bash
curl -X POST http://localhost:8080/webhook/incident \
  -H "Content-Type: application/json" \
  -d '{
    "sys_id": "test-escalate-001",
    "number": "INC0009002",
    "short_description": "SSL certificate expiring in 3 days on payments-api.internal",
    "description": "SSL certificate for payments-api.internal expires in 72 hours. Automated renewal failed.",
    "category": "Security",
    "subcategory": "Certificates",
    "cmdb_ci": "payments-api.internal",
    "assignment_group": "L1-Security-Support",
    "priority": "1",
    "state": "1"
  }'
```

---

## 4. Managing SOPs

### 4.1 SOP Storage

SOPs can be stored in:
1. **ServiceNow Knowledge Base** (production) — retrieved via KB API on each processing cycle
2. **Local JSON files** (demo/dev) — placed in `data/sample_sops/`, auto-loaded when `AGENT_DEMO_MODE=true`

### 4.2 Adding a New SOP

1. Create a JSON file following the schema below
2. Place it in `data/sample_sops/` (demo) or upload to ServiceNow KB (production)
3. Required fields: `sop_id`, `title`, `keywords`, `steps`
4. Each step requires: `step_id`, `step_type`, `parameters`, `on_success`, `on_failure`
5. Test in demo mode before deploying to production (see §4.4)

### 4.3 Step Parameters Reference

**SPLUNK_SEARCH**
```json
{ "query": "index=app_logs sourcetype=payment ERROR", "time_range": "-1h" }
```

**MQ_CHECK**
```json
{ "queue_manager": "PROD.QM1", "queue": "PAYMENT.IN", "action": "depth" }
```
Actions: `depth` · `status` · `browse`

**FILE_CHECK**
```json
{ "unc_path": "//fileserver/logs/app/payment.log", "pattern": "ERROR|FATAL", "last_n_lines": 100 }
```

**AUTOSYS_STATUS**
```json
{ "job_name": "BATCH_PAYMENT_001", "action": "status" }
```
Actions: `status` · `dependencies`

**DECISION**
```json
{ "rule": "any_failed" }
```
Rules: `any_failed` · `all_success`

**DYNATRACE_VM_CHECK**
```json
{ "host_name": "app-server-01", "host_group": "production" }
```

**DYNATRACE_METRICS**
```json
{ "metric_selector": "builtin:host.cpu.usage", "entity_selector": "type(HOST),entityName(app-server-01)", "time_range": "now-1h" }
```

**WEB_UI_CHECK**
```json
{
  "url": "https://app.example.com/dashboard",
  "check_type": "click_path",
  "click_steps": [
    { "action": "click", "selector": "#login-btn", "name": "click-login" },
    { "action": "assert_text", "value": "Welcome", "name": "verify-welcome" }
  ]
}
```

**MAINFRAME_CHECK**
```json
{ "job_name": "BEIM_ASYNC_JOB01", "expected_status": "inact ok", "action": "async_status" }
```

**NOTE**
```json
{ "text": "Beginning investigation for {cmdb_ci}" }
```

### 4.4 Testing a New SOP

```bash
# 1. Start backend in demo mode
export AGENT_DEMO_MODE=true && export LLM_ENABLED=false
python -m src.l1_agent.main

# 2. Fire an incident matching the new SOP keywords
curl -X POST http://localhost:8080/webhook/incident \
  -H "Content-Type: application/json" \
  -d '{ "sys_id": "test-001", "number": "INC9999", ... }'

# 3. Check the result
curl http://localhost:8080/api/incidents/INC9999

# 4. Watch live in the dashboard at http://localhost:5173 → Activity Feed
```

---

## 5. Incident History & Persistence

### 5.1 How History is Stored

Every completed incident execution (resolved or escalated) is written to:
- **In-memory deque** — capped at 1 000 records, available instantly to API queries
- **`data/incident_history.jsonl`** — JSONL file, one record per line, loaded on startup so history survives restarts

### 5.2 Viewing History

```bash
# Via the dashboard
# Navigate to http://localhost:5173 → Incident Feed

# Via API
curl "http://localhost:8080/api/incidents?per_page=20&outcome=escalated"

# Raw file (most recent at bottom)
tail -20 data/incident_history.jsonl | python -m json.tool
```

### 5.3 Clearing History

```bash
# Stop the service first, then clear the file
> data/incident_history.jsonl
# Restart the service
```

---

## 6. Troubleshooting

### 6.1 Common Issues

| Symptom | Likely Cause | Resolution |
|---------|-------------|------------|
| No incidents picked up | Wrong assignment group | Check `SERVICENOW_ASSIGNMENT_GROUP` matches the queue |
| All incidents escalate — `no_sop` reason | SOP cache empty (ServiceNow unreachable) | Set `AGENT_DEMO_MODE=true` to pre-load sample SOPs, or check ServiceNow connectivity |
| All incidents escalate — `no_sop_ai` reason | MockLLM returns low confidence | Switch to rule-based mode: `LLM_ENABLED=false` |
| Low confidence escalations | Keywords don't match | Add `applicable_services`, `applicable_categories`, `applicable_assignment_groups` to SOP |
| Dashboard shows no data | History store empty | Fire incidents via webhook; history is only written after processing completes |
| SSE Activity Feed not connecting | Backend not running or CORS issue | Verify backend is on `:8080`; check browser console for CORS errors |
| `work_note_callback failed` in logs | ServiceNow unreachable | Expected in demo mode — processing continues, work notes are skipped |
| Frontend shows "Reconnecting…" | Backend stopped or `/api/stream` unreachable | Restart backend; frontend auto-reconnects within 3s |
| Adapter timeout | Network/firewall issue | Check connectivity; review circuit breaker state |
| Duplicate work notes | Idempotency check bypassed | Verify `sys_id` is unique per incident; check `processed_ids` set |
| CrewAI: `AuthenticationError` | Missing Anthropic API key | Set `ANTHROPIC_API_KEY` or `CREWAI_API_KEY` env var |
| CrewAI: agents return wrong model | `CREWAI_MODEL` not a valid Anthropic ID | Use a valid model ID e.g. `claude-sonnet-4-6`, `claude-3-5-sonnet-20241022` |
| CrewAI: incident `FAILED` outcome | Agent hit `CREWAI_MAX_ITER` limit | Increase `CREWAI_MAX_ITER` (default 15) or check `CREWAI_VERBOSE=true` logs |
| CrewAI: `list_sops` returns "No SOPs loaded" | `register_sops()` called before SOPs loaded | Ensure `AGENT_DEMO_MODE=true` so sample SOPs are pre-loaded at startup |
| CrewAI: blocking event loop warning | `run_in_executor` not wrapping kickoff | This is handled automatically by `CrewIncidentProcessor` — do not call `kickoff()` directly from async code |

### 6.2 Log Analysis

Logs are structured JSON. Filter by correlation ID (incident number):

```bash
# All logs for a specific incident
grep "INC0012345" agent.log | python -m json.tool

# All errors
python -c "
import sys, json
for line in open('agent.log'):
    try:
        e = json.loads(line)
        if e.get('level') == 'ERROR': print(json.dumps(e, indent=2))
    except: pass
"

# SOP matching decisions
grep "sop_matcher" agent.log | python -m json.tool
```

### 6.3 Dashboard Debugging

```bash
# Check all API endpoints respond
curl http://localhost:8080/api/dashboard/summary | python -m json.tool
curl "http://localhost:8080/api/incidents?per_page=5" | python -m json.tool
curl http://localhost:8080/api/sops/stats | python -m json.tool

# Test SSE stream (should print heartbeat lines every 25s)
curl -N http://localhost:8080/api/stream
```

### 6.4 Circuit Breaker Recovery

If an adapter's circuit breaker is open:
1. Check the tool endpoint is reachable
2. Wait for the reset timeout (default: 60s)
3. The next request acts as a probe — if it succeeds the breaker closes
4. If persistent, check credentials and network configuration

### 6.5 Metrics

```bash
# JSON snapshot (counters, histograms, gauges)
curl http://localhost:8080/health | python -m json.tool

# Prometheus text format (for Grafana scraping)
curl http://localhost:8080/metrics
```

Key metrics to monitor:

| Metric | Type | Description |
|--------|------|-------------|
| `incidents.received` | Counter | Total incidents received |
| `incidents.resolved` | Counter | Total auto-resolved |
| `incidents.escalated{reason=*}` | Counter | Escalations by reason |
| `incidents.failed` | Counter | Failed executions |
| `sop.executions{outcome=*}` | Counter | SOP runs by outcome |
| `sop.execution_duration_ms` | Histogram | End-to-end resolution time |

---

## 7. Execution Mode Operations

### 7.1 Mode Comparison

| | Rule-Based | AI/LLM | CrewAI |
|-|-----------|--------|--------|
| **Env var** | `LLM_ENABLED=false` | `LLM_ENABLED=true` | `CREWAI_ENABLED=true` |
| **API key required** | No | Yes (OpenAI-compat.) | Yes (Anthropic) |
| **SOP selection** | Keyword/CI/category scoring | `AIAnalyzer.select_sop()` | ReviewAgent via `list_sops` |
| **Step execution** | Deterministic sequential | LLM tool-calling loop (≤15 iter) | ResolutionAgent tool-calling |
| **Processor class** | `IncidentProcessor` | `IncidentProcessor` | `CrewIncidentProcessor` |
| **Best for** | Demo, CI, no LLM budget | Flexible unstructured incidents | Showcasing agentic reasoning |

### 7.2 Switching Modes

```bash
# Rule-based (no API key, recommended for demo/testing)
export LLM_ENABLED=false
python -m src.l1_agent.main

# AI/LLM mode
export LLM_ENABLED=true
export LLM_ENDPOINT=https://api.openai.com/v1
export LLM_API_KEY=sk-...
python -m src.l1_agent.main

# CrewAI mode (overrides LLM_ENABLED)
export CREWAI_ENABLED=true
export CREWAI_MODEL=claude-sonnet-4-6
export ANTHROPIC_API_KEY=sk-ant-...
python -m src.l1_agent.main
```

If the LLM endpoint is unreachable at runtime in AI/LLM mode, the agent automatically falls back to rule-based mode for that incident.

### 7.3 How AI/LLM Mode Decides

1. **Incident analysis** — LLM reads the ticket and produces a preliminary root-cause analysis
2. **SOP selection** — LLM calls `select_sop` tool, returning the best SOP with confidence score and rationale
3. **Execution loop** — LLM drives tool calls (`splunk_search`, `mq_check`, `dynatrace_vm_check`, etc.) up to 15 iterations
4. **Conclusion** — LLM calls `resolve_incident` or `escalate_to_l2`

### 7.4 How CrewAI Mode Decides

```
Incident JSON
  → TriageAgent     (no tools)   — structured triage report
  → ReviewAgent     (list_sops, get_sop_details) — selected SOP + confidence
  → ResolutionAgent (7 tool adapters) — per-SOP investigation + OUTCOME verdict
  → ResolverAgent   (no tools)   — work note + "OUTCOME: RESOLVED/ESCALATED"
```

The resolver agent's final output is parsed for `OUTCOME: RESOLVED` or `OUTCOME: ESCALATED` to determine the `ExecutionOutcome`. Each agent task's raw output is stored as a `StepResult` in the `ExecutionSummary`, so the Incident Detail view in the dashboard shows full agent deliberation.

### 7.5 Demo Mode with Mock LLM

```bash
# Standalone demo — rule-based path, no running server needed
python -m src.l1_agent.demo
```

Uses mock adapters and pre-loaded SOPs. Does not require any API key. Fires a batch of test incidents and prints execution summaries.

---

## 8. Maintenance

### 8.1 Updating SOPs

1. Modify the SOP JSON in ServiceNow KB or `data/sample_sops/`
2. **No agent restart required** in production (SOPs fetched from ServiceNow on each incident)
3. In demo mode, restart the backend to pick up changes to `data/sample_sops/`
4. Always test changes in demo mode first (see §4.4)

### 8.2 Updating Credentials

1. Update the environment variable or vault entry
2. Restart the backend: `docker restart l1-agent`
3. Verify with health check: `curl http://localhost:8080/health`

### 8.3 Deploying Frontend Updates

```bash
cd frontend
npm run build          # rebuilds frontend/dist/
# If serving via backend static routes, restart backend
# If using a separate nginx/CDN, deploy dist/ contents
```

### 8.4 Scaling

- Increase `AGENT_MAX_CONCURRENT_INCIDENTS` for higher throughput
- Deploy multiple backend instances behind a load balancer
- For very high volume, migrate to event-driven intake (Kafka/RabbitMQ consumer)
- `IncidentHistoryStore` is in-process — for multi-instance deployments, replace with a shared SQLite or PostgreSQL store

---

## 9. Security Checklist

- [ ] Service account uses least-privilege permissions for all integrations
- [ ] All secrets stored in vault — not in environment variables in production
- [ ] `LLM_API_KEY` injected via secrets vault, never in code or logs
- [ ] `WINDOWS_SHARE_ALLOWED_PREFIXES` configured to restrict file access scope
- [ ] `AUTOSYS_ALLOWED_COMMANDS` limited to read-only (`autorep`) only
- [ ] `WEBUI_ALLOWED_DOMAINS` restricted to approved internal applications
- [ ] CORS `FRONTEND_ORIGINS` restricted to the dashboard hostname in production (not `*`)
- [ ] Network policies restrict backend egress to required tool endpoints only
- [ ] Structured logs reviewed for accidental secret exposure before shipping to SIEM
- [ ] Audit logs (incident history JSONL) retained per compliance requirements
- [ ] `data/incident_history.jsonl` excluded from version control (add to `.gitignore`)
