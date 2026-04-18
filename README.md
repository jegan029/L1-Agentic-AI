# L1 Virtual Engineer Agent

## Overview

A production-ready L1 support agent that automates incident triage and resolution by executing Standard Operating Procedures (SOPs) from ServiceNow. The agent receives incidents, matches them to the correct SOP/runbook, executes diagnostic steps using approved integrations (Splunk, IR360/MQ, Autosys, Windows shares), and updates the incident with evidence and outcomes.

## Key Capabilities

- **Incident Intake**: Webhook listener + polling worker for ServiceNow incidents (idempotent, deduplicated)
- **SOP Matching**: Keyword/regex + CI/category/assignment group scoring with confidence thresholds
- **Step Execution Engine**: Executes SOP steps sequentially with branching, retries, and circuit breakers
- **Tool Adapters**: Splunk REST API, IR360 MQ checks, Autosys CLI (read-only), Windows share log reader
- **Evidence Collection**: Structured audit trail with timestamps, tool outputs, and sanitised evidence
- **Escalation**: Automatic escalation to L2 when confidence is low, access fails, SOP is missing, or remediation requires approval
- **Safety**: Read-only by default; approval gates for any write/change actions

## Architecture

```
┌──────────────┐      ┌──────────────┐      ┌──────────────────┐
│  ServiceNow  │─────▶│  Incident    │─────▶│  SOP Matcher     │
│  (webhook/   │      │  Processor   │      │  (confidence     │
│   polling)   │      │              │      │   scoring)       │
└──────────────┘      └──────┬───────┘      └────────┬─────────┘
                             │                       │
                      ┌──────▼───────────────────────▼─────────┐
                      │         SOP Execution Engine            │
                      │  ┌─────────┬──────────┬──────────────┐ │
                      │  │ Splunk  │  IR360   │  Autosys     │ │
                      │  │ Adapter │  MQ Adpt │  CLI Runner  │ │
                      │  ├─────────┼──────────┼──────────────┤ │
                      │  │ WinShare│ Decision │  NOTE        │ │
                      │  │ Reader  │ Engine   │  Handler     │ │
                      │  └─────────┴──────────┴──────────────┘ │
                      └──────────────────┬─────────────────────┘
                                         │
                      ┌──────────────────▼─────────────────────┐
                      │  ServiceNow Updater                    │
                      │  (work notes, evidence, state changes) │
                      └────────────────────────────────────────┘
```

## Project Structure

```
├── src/l1_agent/
│   ├── main.py                    # Service entry point (webhook + poller)
│   ├── demo.py                    # Demo mode with sample data
│   ├── config/settings.py         # Environment-based configuration
│   ├── models/
│   │   ├── incident.py            # ServiceNow incident model
│   │   ├── sop.py                 # SOP/runbook + step type models
│   │   └── evidence.py            # Step results + execution summary
│   ├── clients/
│   │   └── servicenow_client.py   # ServiceNow REST API client
│   ├── engine/
│   │   ├── sop_matcher.py         # SOP matching with confidence scoring
│   │   ├── sop_parser.py          # Parse SOPs from ServiceNow or JSON
│   │   ├── executor.py            # Step execution engine
│   │   └── incident_processor.py  # End-to-end incident orchestration
│   ├── adapters/
│   │   ├── base.py                # Adapter interface
│   │   ├── splunk_adapter.py      # Splunk REST API client
│   │   ├── ir360_adapter.py       # IR360 MQ adapter (API + CLI)
│   │   ├── autosys_adapter.py     # Autosys CLI runner (read-only)
│   │   ├── windows_share_adapter.py # SMB/UNC log file reader
│   │   └── mock_adapters.py       # Mock adapters for demo/testing
│   └── utils/
│       ├── logging.py             # Structured JSON logging + redaction
│       ├── retry.py               # Exponential backoff + circuit breaker
│       └── metrics.py             # In-process metrics collector
├── data/sample_sops/              # Example SOP definitions (JSON)
├── tests/
│   ├── unit/                      # Unit tests with mocks
│   └── integration/               # Integration test scaffolding
├── ARCHITECTURE.md                # Design document
├── RUNBOOK.md                     # Operational runbook
├── pyproject.toml
├── Dockerfile
└── .env.example
```

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the demo (no real credentials needed)
python -m src.l1_agent.demo

# Run the full service (requires ServiceNow config)
cp .env.example .env
# Edit .env with your credentials
python -m src.l1_agent.main

# Run tests
pytest tests/ -v
```

## Demo Mode

The demo processes a sample MQ queue depth incident through a complete SOP:

```bash
python -m src.l1_agent.demo
```

This demonstrates:
1. Incident parsing from ServiceNow payload
2. SOP matching with confidence scoring
3. Step-by-step execution (MQ checks, Splunk search, Autosys status)
4. Decision branching based on results
5. Work note generation and incident update payload

## Configuration

See `.env.example` for all configuration variables. Key settings:

| Variable | Description |
|----------|-------------|
| `SERVICENOW_BASE_URL` | ServiceNow instance URL |
| `SERVICENOW_USERNAME` / `PASSWORD` | API credentials |
| `SPLUNK_BASE_URL` / `TOKEN` | Splunk REST API access |
| `IR360_BASE_URL` / `API_KEY` | IR360 MQ monitoring |
| `AGENT_SOP_CONFIDENCE_THRESHOLD` | Minimum SOP match confidence (default: 0.6) |
| `AGENT_DEMO_MODE` | Use mock adapters (default: false) |

## SOP Schema

SOPs are defined as JSON with the following structure:

```json
{
  "sop_id": "SOP-MQ-001",
  "title": "MQ Queue Depth High",
  "keywords": ["queue depth", "mq"],
  "applicable_services": ["PaymentService"],
  "applicable_categories": ["Middleware"],
  "steps": [
    {
      "step_id": "step-1",
      "step_type": "MQ_CHECK",
      "parameters": {"queue_manager": "QM1", "queue": "Q1", "action": "depth"},
      "on_success": "step-2",
      "on_failure": "step-escalate"
    }
  ],
  "escalation_criteria": {
    "escalate_on_access_denied": true,
    "escalate_on_write_action": true
  }
}
```

Supported step types: `SPLUNK_SEARCH`, `MQ_CHECK`, `FILE_CHECK`, `AUTOSYS_STATUS`, `DECISION`, `NOTE`

## Security

- Read-only by default; no destructive actions without explicit SOP permission + approval gate
- Secrets loaded from environment variables (vault integration for production)
- Sensitive fields redacted from structured logs
- UNC paths validated against allow-list
- Autosys commands restricted to read-only allow-list
- CLI inputs validated against injection patterns

## License

MIT
