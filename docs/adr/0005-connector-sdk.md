# ADR 0005: Connector SDK and registry

Status: accepted and built in Phase 3, with the differences listed under "As built in Phase 3" at the end. Engineering decision.

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

## As built in Phase 3

Built as decided: the manifest model and its lint; a registry that refuses to exist with a bad manifest; SOP steps that reference capability ids, checked by the validator at submission and again at publication; closed parameter schemas and allow lists enforced before connector code runs; credential references resolved per call through a short-lived cache; the write entry point refusing every call; `test_connection` and `health`; and the ServiceNow, ServiceNow knowledge base and Git connectors.

Differences from the decision above, each an engineering choice:

| Decision | As built | Why |
|---|---|---|
| Connectors register through Python entry points; the image carries a signed list of permitted versions | A first-party list in `connectors/catalog.py` | Only first-party connectors exist. Entry points and a signed list are packaging work and move to Phase 6. |
| The seven adapters are ported onto the SDK's HTTP client | Each is wrapped by a connector that declares the manifest and delegates to the original adapter | The adapters' HTTP and parsing code cannot be verified against real systems here. Rewriting it would add risk with nothing to check it against. The SDK HTTP client is used by the ServiceNow connectors, which can be checked against a developer instance. |
| Retry only for the error classes a capability lists; circuit breaker in the SDK | Retries and breakers stay in the engine (`engine/executor.py`); manifests carry `retryable_errors` but nothing reads it yet. Breakers are now per tenant and connector | Every capability today is an idempotent read, so retrying is safe. The distinction matters for writes and is built with them in Phase 4. |
| Audit hooks in the SDK record every call | The engine's `step.started` and `step.completed` events record every call made in a run; `connection.tested` records a test | One record per call already exists. A second in the SDK would duplicate it. |
| A write requires an action grant minted by the policy engine | A write is refused unconditionally, whatever is passed as a grant | There is no policy engine to mint one. Phase 4 replaces the refusal with the grant check. |
| ITSM interface in neutral terms (ticket, state category, assignee group) | The interface keeps the method names the engine already used (`add_work_note`, `update_incident`, `resolve_incident`) and adds `claim` | With one ITSM system there is nothing to generalise from. Rename when the second one is added. |
| `test_connection` reports per-scope results | It reports the health check as one result | Per-scope probing needs calls that cannot be verified here. The result type already carries a list. |
| Browser connector in a sandboxed container | Unchanged local Selenium; marked `preview` in its manifest and not in the default image | As the decision allows: it stays out of the default image until the sandbox exists. |
| Mainframe navigation scripts selected by name | One script per connection (`default_navigation`); a step cannot supply keystrokes | Named scripts need a connection editor. Phase 6. |

Two additions the decision did not foresee:

- **A credential that changed is retried once.** When a call fails and the backend now holds a different value from the one that was used, the connector reads the new value and repeats the call. That makes a rotation take effect on the next call even inside the cache lifetime. It is limited to reads; for ServiceNow it applies to any request that was answered `401`, which means the request was not processed.
- **One connection per connector per tenant.** `connections` is unique on tenant and connector. Two instances of the same system for one tenant need a connection name in the key; that is a migration, deferred until someone needs it.

Defects found in the original adapters while writing the contract tests, and fixed: Dynatrace host health read only the first page of hosts and asked about problems on only the first 20, so a large group could be reported healthy with a problem on an unread host; a Dynatrace host name was placed inside a quoted selector unescaped; an IR360 queue or queue manager name was placed in a URL path unchecked; the Autosys status was taken from the last column of `autorep` output, which on real output is the exit code column.
