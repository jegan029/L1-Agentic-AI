## L1 Engineer Agent

## Overview

A production-ready autonomous L1 incident resolution platform built for State Street operations. The system receives ServiceNow incidents, matches them to Standard Operating Procedures (SOPs), executes multi-step diagnostic runbooks using approved tool integrations, and resolves or escalates incidents — all without human intervention.

The platform includes a **React + Vite customer-facing dashboard** styled to the State Street brand that showcases real-time agent activity, incident history, SOP performance, and a live activity feed via Server-Sent Events (SSE).

---

## What's New

### Intelligent Escalation Engine (Latest)

A full L2 escalation pipeline has been added, replacing ad-hoc escalation logic with a single, auditable flow:

- **CI-Based L2 Routing** (`escalation/l2_router.py`) — maps the ticket's Configuration Item to the correct L2 team via `config/l2_routing.json`. Supports fnmatch wildcards (`DB*`, `MQ*`, `APP*`) with a configurable default fallback team.
- **Email Notification** (`notifications/email_notifier.py`) — sends a structured escalation email to the resolved L2 team via SMTP/TLS. Reads `SMTP_HOST/PORT/USER/PASSWORD/FROM` from environment. Best-effort: if SMTP is unconfigured, escalation proceeds and a warning is logged.
- **Central Escalation Function** (`escalation/escalator.py`) — single `escalate_to_l2()` coroutine called by all escalation paths (no SOP, low confidence, mid-SOP failure). Handles routing → SNOW assignment group update → email → history record → enriched SSE event in one place.
- **Confidence Threshold Enforcement** — both AI and rule-based paths now guard against `None` confidence; all checks use the single `AGENT_SOP_CONFIDENCE_THRESHOLD` variable.
- **Enriched SSE Escalation Event** — `incident_escalated` events now include `l2_team_name`, `l2_team_email`, and `email_sent`.
- **Bug fix** — `ResolutionMemory.stats()` had a threading deadlock (re-entrant `Lock` acquisition) that surfaced when historical data was present at startup. Fixed by computing all values inline within the single lock acquisition.
- **Unit Tests** — `tests/unit/test_l2_router.py` (11 tests) and `tests/unit/test_confidence_threshold.py` (5 tests).

### Frontend Dashboard (React + Vite)
A full customer-facing showcase dashboard at `frontend/` with:
- **Live Dashboard** — KPI cards, outcome donut chart, escalation reasons bar chart, 7-day activity trend
- **Incident Feed** — searchable/filterable table of all processed incidents; click any row to drill into the step-by-step execution trace
- **SOP Performance** — per-SOP resolution rates with sortable columns and inline progress bars
- **Activity Feed** — real-time SSE stream of every agent action; now shows routed L2 team and email status on escalation events
- State Street brand design: navy/blue colour scheme, official logo, Inter typography

### Backend Extensions
- **SSE Event Bus** (`events/event_bus.py`) — real-time fan-out of agent events to all connected browsers
- **Incident History Store** (`store/incident_history.py`) — in-memory deque (1 000 cap) with JSONL persistence across restarts
- **SOP Stats** (`store/sop_stats.py`) — live per-SOP aggregated metrics computed from history
- **Dashboard API** (`api/dashboard.py`) — `GET /api/dashboard/summary`
- **Incidents API** (`api/incidents.py`) — `GET /api/incidents`, `GET /api/incidents/{n}`
- **SOPs API** (`api/sops.py`) — `GET /api/sops/stats`
- **SSE Stream** (`api/stream.py`) — `GET /api/stream`
- **CORS** via `aiohttp-cors` (all origins, demo mode)
- **Demo SOP pre-loading** — in demo mode, sample SOPs from `data/sample_sops/` are loaded automatically so incidents resolve without ServiceNow

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    React + Vite Frontend                        │
│  LiveDashboard │ IncidentFeed │ SopPerformance │ ActivityFeed   │
│       ↑ polls /api/*                    ↑ SSE /api/stream       │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP / SSE
┌──────────────────────────▼──────────────────────────────────────┐
│                  aiohttp Backend  :8080                         │
│                                                                 │
│  POST /webhook/incident   GET /health    GET /metrics           │
│  GET  /api/dashboard/summary             GET /api/stream        │
│  GET  /api/incidents[/{n}]               GET /api/sops/stats    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Incident Processor                         │   │
│  │  ServiceNow → SOP Matcher → SOPExecutor → Conclusion    │   │
│  │       ↓ publishes SSE events          ↓ writes history  │   │
│  │  EventBus ──────────────────▶ IncidentHistoryStore      │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Adapters: Splunk │ IR360/MQ │ Autosys │ Dynatrace │ Mainframe  │
│            WindowsShare │ WebUI Scraper │ Mock (demo mode)      │
└─────────────────────────────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                     ServiceNow                                  │
│         (incidents, SOPs, work notes, state updates)            │
└─────────────────────────────────────────────────────────────────┘
```

**Two execution modes** selected by `LLM_ENABLED` env var:

| Mode | SOP Selection | Step Execution |
|------|--------------|----------------|
| **AI-driven** (default) | LLM selects SOP with reasoning | LLM drives tool calls autonomously |
| **Rule-based** (fallback) | Keyword/regex scoring | Deterministic sequential steps |

---

## Project Structure

```
L1-Agentic AI/
├── L1_Agentic_AI_Solution/            ← Python backend
│   ├── src/l1_agent/
│   │   ├── main.py                    # Service entry point (webhook + poller + API)
│   │   ├── demo.py                    # Standalone demo (no credentials needed)
│   │   ├── config/settings.py         # Environment-based configuration
│   │   ├── models/
│   │   │   ├── incident.py            # ServiceNow incident model
│   │   │   ├── sop.py                 # SOP + step type definitions
│   │   │   └── evidence.py            # StepResult + ExecutionSummary
│   │   ├── clients/
│   │   │   └── servicenow_client.py   # ServiceNow REST API client
│   │   ├── engine/
│   │   │   ├── incident_processor.py  # End-to-end orchestration + SSE events
│   │   │   ├── executor.py            # Rule-based step execution engine
│   │   │   ├── sop_matcher.py         # Keyword/CI/category confidence scoring
│   │   │   ├── sop_parser.py          # Parse SOPs from ServiceNow or JSON
│   │   │   └── resolution_memory.py   # Learning loop (historical outcomes)
│   │   ├── ai/
│   │   │   ├── analyzer.py            # LLM-based SOP selection
│   │   │   ├── ai_executor.py         # LLM tool-calling execution loop
│   │   │   ├── llm_client.py          # OpenAI-compatible LLM client
│   │   │   └── mock_llm.py            # Mock LLM for demo mode
│   │   ├── adapters/
│   │   │   ├── base.py                # Adapter interface
│   │   │   ├── splunk_adapter.py      # Splunk REST API
│   │   │   ├── ir360_adapter.py       # IR360 / IBM MQ
│   │   │   ├── autosys_adapter.py     # Autosys CLI (read-only)
│   │   │   ├── dynatrace_adapter.py   # Dynatrace VM + metrics
│   │   │   ├── mainframe_adapter.py   # TN3270 mainframe
│   │   │   ├── webui_scraper_adapter.py # Selenium headless browser
│   │   │   ├── windows_share_adapter.py # SMB/UNC log reader
│   │   │   └── mock_adapters.py       # Mock adapters for demo/testing
│   │   ├── escalation/
│   │   │   ├── l2_router.py           # NEW: CI → L2 team routing (fnmatch wildcards)
│   │   │   └── escalator.py           # NEW: central escalate_to_l2() function
│   │   ├── notifications/
│   │   │   └── email_notifier.py      # NEW: SMTP/TLS escalation email sender
│   │   ├── events/
│   │   │   └── event_bus.py           # NEW: async SSE fan-out broadcaster
│   │   ├── store/
│   │   │   ├── incident_history.py    # NEW: in-memory + JSONL history store
│   │   │   └── sop_stats.py           # NEW: per-SOP aggregated stats
│   │   ├── api/
│   │   │   ├── dashboard.py           # NEW: GET /api/dashboard/summary
│   │   │   ├── incidents.py           # NEW: GET /api/incidents[/{n}]
│   │   │   ├── sops.py                # NEW: GET /api/sops/stats
│   │   │   └── stream.py              # NEW: GET /api/stream (SSE)
│   │   ├── ui/
│   │   │   └── sop_editor.py          # Web UI for SOP authoring
│   │   └── utils/
│   │       ├── logging.py             # Structured JSON logging + redaction
│   │       ├── metrics.py             # In-process Prometheus-compatible metrics
│   │       ├── retry.py               # Exponential backoff + circuit breaker
│   │       └── secrets.py             # Vault / AWS SSM integration
│   ├── data/
│   │   ├── sample_sops/               # SOP JSON library (MQ, Autosys)
│   │   └── incident_history.jsonl     # Persisted incident execution history
│   ├── tests/
│   │   ├── unit/
│   │   │   ├── test_l2_router.py      # NEW: 11 tests for CI routing + wildcards
│   │   │   └── test_confidence_threshold.py  # NEW: 5 tests for threshold enforcement
│   │   └── integration/               # Integration test scaffolding
│   ├── ARCHITECTURE.md
│   ├── RUNBOOK.md
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── requirements.txt
│   └── .env.example
│
└── frontend/                          ← NEW: React + Vite dashboard
    ├── src/
    │   ├── App.jsx                    # Root shell + footer
    │   ├── config.js                  # VITE_API_BASE_URL passthrough
    │   ├── context/AgentContext.jsx   # Global state provider
    │   ├── hooks/
    │   │   ├── useAgentMetrics.js     # Polls /api/dashboard/summary every 10s
    │   │   ├── useIncidentHistory.js  # Paginated /api/incidents fetch
    │   │   ├── useSopStats.js         # Polls /api/sops/stats every 30s
    │   │   └── useActivityStream.js   # EventSource to /api/stream (SSE)
    │   ├── components/
    │   │   ├── NavBar.jsx             # State Street branded navigation
    │   │   ├── LiveDashboard.jsx      # KPIs, charts, 7-day trend
    │   │   ├── IncidentFeed.jsx       # Searchable incident table
    │   │   ├── IncidentDetail.jsx     # Slide-over execution trace
    │   │   ├── SopPerformance.jsx     # Sortable SOP stats table
    │   │   ├── ActivityFeed.jsx       # Live SSE event stream
    │   │   ├── KpiCard.jsx            # Metric card component
    │   │   └── StatusBadge.jsx        # Outcome chip component
    │   └── utils/formatters.js        # Duration, priority, time helpers
    ├── public/
    │   └── state-street-logo-final.svg # Official State Street logo
    ├── package.json
    └── vite.config.js                 # Dev proxy: /api/* → localhost:8080
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+

### 1 — Install backend dependencies

```bash
cd L1_Agentic_AI_Solution
pip install -r requirements.txt
```

### 2 — Start the backend (demo mode — no credentials needed)

```bash
cd L1_Agentic_AI_Solution
export AGENT_DEMO_MODE=true
export LLM_ENABLED=false
python -m src.l1_agent.main
# Webhook listener starts on http://localhost:8080
```

Demo mode uses:
- Mock adapters (no real Splunk / MQ / Autosys connections)
- Sample SOPs pre-loaded from `data/sample_sops/`
- Rule-based SOP matching (no LLM API key required)

### 3 — Start the frontend

```bash
cd frontend
npm install
npm run dev
# Opens http://localhost:5173
```

The Vite dev server proxies all `/api/*` requests to `http://localhost:8080` automatically.

### 4 — Fire demo incidents

**Incident that resolves via SOP:**
```bash
curl -X POST http://localhost:8080/webhook/incident \
  -H "Content-Type: application/json" \
  -d '{
    "sys_id": "demo-resolve-001",
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
    "sys_id": "demo-escalate-001",
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

## API Reference

### Existing Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/incident` | Receive a ServiceNow incident webhook |
| `GET` | `/health` | Service health + metrics snapshot |
| `GET` | `/metrics` | Prometheus-compatible metrics |

### Dashboard API (New)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/dashboard/summary` | KPIs, outcome breakdown, escalation reasons, 7-day trend |
| `GET` | `/api/incidents` | Paginated incident history (`?page=&per_page=&search=&outcome=&priority=`) |
| `GET` | `/api/incidents/{incident_number}` | Single incident with full step-by-step execution trace |
| `GET` | `/api/sops/stats` | Per-SOP aggregated resolution statistics |
| `GET` | `/api/stream` | Server-Sent Events stream of real-time agent activity |

#### `/api/stream` Event Types

| Event | Payload Fields |
|-------|---------------|
| `incident_received` | `incident_number`, `short_description`, `priority` |
| `sop_matched` | `incident_number`, `sop_id`, `sop_title`, `confidence` |
| `step_executing` | `incident_number`, `step_id`, `step_type`, `description` |
| `step_done` | `incident_number`, `step_id`, `status`, `output_summary`, `duration_ms` |
| `incident_resolved` | `incident_number`, `sop_id`, `duration_ms` |
| `incident_escalated` | `incident_number`, `reason`, `l2_team_name`, `l2_team_email`, `email_sent` |
| `heartbeat` | `ts` (every 25s, keeps connection alive) |

---

## Configuration

Copy `.env.example` and fill in credentials. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_DEMO_MODE` | `false` | Use mock adapters + pre-load sample SOPs |
| `LLM_ENABLED` | `false` | Enable AI-driven SOP selection and execution |
| `LLM_ENDPOINT` | — | OpenAI-compatible endpoint URL |
| `LLM_API_KEY` | — | LLM API key |
| `AGENT_SOP_CONFIDENCE_THRESHOLD` | `0.6` | Minimum confidence to execute a SOP |
| `AGENT_WEBHOOK_PORT` | `8080` | Backend listening port |
| `SERVICENOW_BASE_URL` | — | ServiceNow instance URL |
| `SERVICENOW_USERNAME` / `_PASSWORD` | — | API credentials |
| `SPLUNK_BASE_URL` / `_TOKEN` | — | Splunk REST API |
| `IR360_BASE_URL` / `_API_KEY` | — | IR360 MQ monitoring |
| `SMTP_HOST` | — | SMTP server for escalation emails |
| `SMTP_PORT` | `587` | SMTP port (STARTTLS) |
| `SMTP_USER` / `SMTP_PASSWORD` | — | SMTP credentials |
| `SMTP_FROM` | (`SMTP_USER`) | Sender address for escalation emails |

---

## SOP Schema

SOPs are JSON files in `data/sample_sops/`. Supported step types:

`SPLUNK_SEARCH` · `MQ_CHECK` · `FILE_CHECK` · `AUTOSYS_STATUS` · `DYNATRACE_VM_CHECK` · `DYNATRACE_METRICS` · `WEB_UI_CHECK` · `MAINFRAME_CHECK` · `DECISION` · `NOTE`

```json
{
  "sop_id": "SOP-MQ-001",
  "title": "MQ Queue Depth High - Investigation and Remediation",
  "keywords": ["queue depth", "mq", "messages not consumed"],
  "applicable_services": ["PaymentService"],
  "applicable_categories": ["Middleware"],
  "applicable_assignment_groups": ["L1-Middleware-Support"],
  "steps": [
    {
      "step_id": "step-1",
      "step_type": "MQ_CHECK",
      "description": "Check queue depth on reported queue",
      "parameters": { "queue_manager": "QMPROD01", "queue": "PAYMENT.REQUEST", "action": "depth" },
      "on_success": "step-2",
      "on_failure": "step-escalate"
    },
    {
      "step_id": "step-2",
      "step_type": "DECISION",
      "description": "Evaluate check results",
      "parameters": { "rule": "any_failed" },
      "on_success": "step-resolve",
      "on_failure": "step-escalate"
    }
  ],
  "escalation_criteria": {
    "escalate_on_access_denied": true,
    "escalate_on_write_action": true
  }
}
```

---

## Running Tests

```bash
cd L1_Agentic_AI_Solution

# All unit tests
pytest tests/unit/ -v

# Skip integration tests (no live credentials needed)
pytest -m "not integration" -v

# With coverage
pytest tests/unit/ --cov=src/l1_agent --cov-report=term-missing

# Lint
ruff check src/ tests/
ruff format src/ tests/
```

---

## Docker

```bash
# Production image
docker build -t l1-agent:latest .

# Run with environment file
docker run --env-file .env -p 8080:8080 l1-agent:latest

# Demo image
docker build --target demo -t l1-agent-demo:latest .
docker run -p 8080:8080 l1-agent-demo:latest
```

---

## Security

- **Read-only by default** — no destructive actions without explicit SOP approval gate
- **Secrets management** — environment variables with optional Vault / AWS SSM integration
- **Log redaction** — sensitive fields (passwords, tokens) stripped from structured JSON logs
- **Input validation** — UNC paths validated against allow-list; Autosys restricted to read-only commands
- **Idempotency** — processed incident IDs tracked to prevent duplicate execution
- **Concurrency** — semaphore-limited (default: 5 concurrent incidents)
- **CORS** — enabled for all origins in demo mode; restrict `FRONTEND_ORIGINS` in production
