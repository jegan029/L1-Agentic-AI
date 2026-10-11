# ADR 0005: Connector SDK and registry

Status: proposed, awaiting review. Engineering decision.

## Context

Adapters are constructed in a hard-coded dictionary in `main.py` and mapped from step types by a second hard-coded dictionary in `executor.py`, a third in `ai_executor.py` and a fourth in `crew/tools.py`. The adapter interface is `execute(parameters: dict)` with no schema, no declared capabilities and no distinction between read and write. Credentials come from process environment variables.

## Decision

### Three families, one base

| Family | Interface | First implementations |
|---|---|---|
| ITSM | list and fetch incidents, claim, add note, update state, reassign, create and read change records, read approvals, verify webhook | ServiceNow. The interface uses neutral terms (ticket, state category, assignee group) so Jira Service Management and BMC Helix can map onto it. |
| Knowledge | list articles changed since a cursor, fetch article with metadata and revision | ServiceNow KB, Git. Confluence and SharePoint follow. |
| Operations | named capabilities invoked with validated parameters | Splunk, Dynatrace, Autosys, IBM MQ (through IR360), mainframe, file share, browser check. Grafana and Geneos follow. |

### Manifest

Each connector ships a manifest, defined as a typed model in code and exported as JSON for the UI and for linting.

```
connector:
  id, version, family, display name
  config schema            (JSON Schema; non-secret settings)
  secret slots             (names and purposes; values are references)
  required scopes          (what to grant in the target system, per capability)
  capabilities:
    - id                   e.g. autosys.job.force_start
      kind                 read | write
      risk tier            T0 | T1 | T2
      parameter schema     (JSON Schema, additionalProperties false)
      output schema
      allow list bindings  which parameters are checked against which tenant allow list
      supports dry run     bool
      idempotent           bool
      retryable errors     list of error classes that are safe to retry
      pre-checks           capability ids, for writes
      verify               capability id plus assertion, required for writes
      rollback             capability id, or "none" with a reason
      timeout
```

A lint test fails the build if a write capability lacks a tier, a verify operation, a closed parameter schema, scopes, or an allow list binding for each target-identifying parameter. T3 is a policy classification for things that must never be done; a connector may not declare a T3 capability at all.

### Registry

Connectors register through Python entry points, discovered at start-up. The registry is the only way to obtain a connector. SOP steps reference capability ids, not adapter keys. The SOP validator checks every referenced capability exists and that the step's parameters satisfy its schema at publication time.

Only first-party connectors are loaded at first. The image carries a signed list of permitted connector distributions and versions. Third-party connectors are a later decision.

### SDK services

The SDK base class provides, so that connectors do not reimplement them:

- An HTTP client with TLS verification on. The option to disable it exists only when demo mode is on and is refused otherwise, at construction.
- Timeouts on every call.
- Retry only for error classes the capability lists as retryable. Writes are not retried unless the capability is idempotent.
- Circuit breaker per connection.
- `test_connection`, which exercises authentication and each required scope and reports per-scope results.
- `health`, which returns healthy, degraded or unavailable with a reason. An error from the target system is never reported as healthy.
- Parameter validation against the schema, then allow list checks, before the connector code runs. An empty allow list denies.
- Secret resolution on each call through a short-lived cache, so rotation takes effect without restart. Secrets are never placed in the process environment or the database.
- Audit hooks that record redacted inputs and outputs for every call.

### Write entry point

A write capability cannot be called with parameters alone. Its signature requires an action grant minted by the policy engine (ADR 0003, point 4). Read capabilities do not need a grant but do need a step context, so they cannot be called outside a run except by `test_connection`.

### Porting the seven adapters

The existing adapters are evolved, not rewritten: their HTTP and parsing code is sound in most cases. The defects found in review are fixed during the port (list in the delivery plan, Phase 1 and Phase 3). Two notes:

- The browser connector should move from Selenium with `--no-sandbox` to a browser running in a separate, sandboxed, network-restricted container. If that is not ready, the connector stays out of the default image.
- The mainframe connector accepts only navigation scripts stored with the connection by a tenant administrator, selected by name. TLS is required; port 23 without TLS is refused outside demo mode.

## Consequences

- SOPs become coupled to capability ids and versions. A capability's schema change is a breaking change that requires republishing affected SOPs; the registry must report which published SOPs use a capability.
- More up-front work per connector, repaid by uniform policy, audit and UI.

## Alternatives considered

- Out-of-process connectors over gRPC. Better isolation, more operational weight. The runner split in ADR 0003 already gives a process and network boundary; revisit if third-party connectors are allowed.
- Model Context Protocol servers as the connector layer. Rejected for the execution path: the protocol is designed for model-driven tool use, which ADR 0001 rules out, and it does not carry tiers, verify or rollback.
