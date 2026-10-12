# Target architecture

Status: written in Phase 0 as the intended end state after Phase 6, and kept as written. It is the design, not a description of the code: [ARCHITECTURE.md](../ARCHITECTURE.md) says what was built, and section 11 lists where the two differ.

Related: [delivery plan](delivery-plan.md), [threat model](threat-model.md), [controls matrix](controls-matrix.md), ADRs in [adr/](adr/).

## 1. Principles

1. **The SOP version decides what runs.** A published, hashed SOP version determines every capability call and its parameter template. A model never selects a tool or supplies a raw argument (ADR 0001).
2. **Fail closed.** No outcome has a default. RESOLVED requires a passed verification. Anything unexpected becomes NEEDS_HUMAN (ADR 0002).
3. **Every write has a policy decision.** A write capability cannot be invoked without a grant minted by the policy engine, and the grant is bound to the exact action and parameters.
4. **Tenant isolation at the data layer.** Application bugs must not be able to cross tenants (ADR 0004).
5. **The audit log can stand alone.** One run can be reconstructed from audit events without the operational tables (ADR 0007).
6. **Secrets stay where the customer keeps them.** The product stores references.
7. **Works without a model.** Model assistance improves matching and authoring; it is never required for safe operation.

## 2. System view

```
                         Customer identity provider (OIDC / SAML)
                                        |
   Users (browser) ---------------------+
        |
        v
+----------------------------- Control plane -----------------------------+
|                                                                         |
|  React UI  <->  API (deny-by-default routes, RBAC, CSRF, rate limits)   |
|                                                                         |
|  SOP lifecycle      Policy engine      Approvals      Kill switch       |
|  (draft, review,    (tier, autonomy,   (bound to      (global,          |
|   publish, hash)     windows, limits)   action hash)   per tenant)      |
|                                                                         |
|  State machine workers  <-  durable job queue  <-  intake (webhook,     |
|        |                                           poller, claim)       |
|        |           Advisor (rank, extract, interpret, draft)            |
|        |              |  redaction, schema validation, budgets          |
|        |              v                                                 |
|        |           Model provider (per tenant, optional)                |
|        |                                                                |
|  PostgreSQL (RLS)   Audit log (hash chain)   Evidence store (WORM)      |
+--------|-------------------------|------------------------|-------------+
         |                         |                        |
         | runner protocol         | export                 |
         | (outbound from runner,  v                        |
         |  mTLS, signed grants)   Customer SIEM            |
         v
+----------------------------- Runner ------------------------------------+
|  Connector registry and SDK                                             |
|  Local ceiling: max tier, capability and target allow lists, kill switch|
|  Secret resolution from the customer's secrets manager                  |
|                                                                         |
|  Operations connectors: Splunk, Dynatrace, Autosys, IBM MQ, mainframe,  |
|  file share, browser check (Grafana, Geneos to follow)                  |
+-------------------------------------------------------------------------+
         |
         v
   Customer systems

ITSM connector (ServiceNow) and knowledge connectors (ServiceNow KB, Git,
Confluence, SharePoint) run in whichever plane can reach the target.
```

In the single-tenant deployment both planes run in the customer's cluster. In the hosted option the control plane is ours and the runner is theirs (ADR 0003).

## 3. Module layout

The existing layout is kept where it fits. "Evolve" means the file is changed in place; "rewrite" means replacement is cheaper and the ADR says why.

```
src/l1_agent/
  api/            rewrite    FastAPI routers, route-permission registry (ADR 0006)
  auth/           new        OIDC, SAML, sessions, CSRF, API tokens, role mapping
  tenancy/        new        tenant context, RLS session setup
  db/             new        SQLAlchemy models, Alembic migrations, repositories
  queue/          new        job table, worker loop, outbox
  engine/
    state_machine.py   new        transition table as data, transition function
    runner.py          evolve     from executor.py: step dispatch
    sop_matcher.py     evolve     candidate generation
    expressions.py     new        typed rule and assertion language
    parameters.py      new        typed parameter binding and validation
    incident_processor.py  rewrite as a thin job handler
    resolution_memory.py   remove (replaced by queries)
  sop/            new        schema, validator (graph search), versions, hashing,
                             ingestion, draft generation, review workflow
  connectors/
    sdk/          new        base classes, manifest model, registry, HTTP client,
                             secrets, retry, breaker, health, lint
    itsm/servicenow/         evolve from clients/servicenow_client.py
    knowledge/               new   servicenow_kb, git, confluence, sharepoint
    ops/                     evolve from adapters/: splunk, dynatrace, autosys,
                             ibm_mq, mainframe, fileshare, browser
    mock/                    evolve from adapters/mock_adapters.py
  actions/        new        tiers, policy engine, autonomy, approvals, grants,
                             change linkage, kill switch, action pipeline
  advisor/        evolve from ai/: provider interface, providers, four tasks,
                             output schemas, redaction, budgets, versioned prompts
  audit/          new        chain, append, verify, reconstruct, export, retention,
                             evidence store
  runner/         new        protocol, runner agent, local ceiling
  escalation/     evolve     routing from tenant configuration
  notifications/  evolve
  events/         evolve     event bus fed from the outbox
  config/         evolve     bootstrap settings only; tenant settings live in the DB
  utils/          evolve     logging with message redaction; secrets.py replaced
  crew/           remove
  ui/sop_editor.py  remove
frontend/         evolve     sign-in, role-aware navigation, SOP review, approvals,
                             connector wizard, policy and autonomy, audit viewer
evals/            new        golden incidents, injection corpus, runner
deploy/           new        Helm chart, runner image, compose file for demo
```

## 4. Incident run state machine

Run-level states. The step-level graph inside EXECUTING comes from the SOP version.

```
RECEIVED --claim ok--> CLAIMED --> MATCHING --match--> BINDING --> EXECUTING
   |                      |           |                   |            |
   | duplicate            | claim     | none / low        | unbound /  |
   v                      | lost      | confidence        | invalid    |
 (no run)                 v           v                   v            |
                       (no run)   ESCALATED          NEEDS_HUMAN       |
                                                                       |
   +-------------------------------------------------------------------+
   |
   |  per step:
   |    CHECK / DECISION / NOTE ........ stay in EXECUTING
   |    ACTION ......................... POLICY_CHECK
   |    VERIFY ......................... VERIFYING
   |    ESCALATE ....................... ESCALATED
   |    RESOLVE ........................ only if entered from VERIFYING pass
   v
POLICY_CHECK --deny----------------------------> ESCALATED (reason recorded)
     |--shadow--> record "would do", continue as if not executed
     |--recommend--> post recommendation, ESCALATED
     |--needs approval--> AWAITING_APPROVAL --denied / expired--> ESCALATED
     |                          |--approved--> ACTING
     |--auto (allow-listed)--> ACTING

ACTING: pre-checks -> dry run -> execute -> (record) --> VERIFYING
VERIFYING --pass--> EXECUTING (next step) or RESOLVED via RESOLVE step
          --fail--> ROLLING_BACK (if declared) --> ESCALATED

Any state --exception, timeout, unknown input, budget--> NEEDS_HUMAN
Any state --kill switch or operator stop--> ABORTED
```

Terminal: RESOLVED, ESCALATED, NEEDS_HUMAN, ABORTED.

The transition table is a data structure, not control flow, so tests can search it (ADR 0002).

In shadow mode an action is never executed, so its verification cannot pass because of the action. A shadow run therefore ends ESCALATED with the record of what would have been done, unless the symptom is already absent.

## 5. SOP model and lifecycle

### Schema

An SOP version contains: metadata; applicability (services, categories, groups, keywords); **parameters**; **steps**; default autonomy ceiling; and the standard change template it maps to, if any.

A parameter has a name, a JSON Schema type and constraints, a source and an optional allow list binding.

| Source | Meaning |
|---|---|
| `ticket_field` | A named field from the ITSM record |
| `extraction` | A pattern over ticket text declared by the author, optionally assisted by the advisor; the result is always validated |
| `lookup` | A read capability call, for example a CMDB query |
| `constant` | Fixed in the SOP version |

Steps reference parameters by name in templates. Templates are written by the SOP author and rendered by the engine; for query languages (SPL, DQL) the renderer escapes values for that language and the parameter schema restricts the character set. A parameter that fails validation stops the run with NEEDS_HUMAN and the reason.

Step types: CHECK, DECISION, ACTION, VERIFY, NOTE, ESCALATE, RESOLVE. CHECK, ACTION and VERIFY name a connector capability id.

### Lifecycle

```
ingested article --> DRAFT --> IN_REVIEW --> APPROVED --> PUBLISHED (immutable)
                       ^           |                         |
                       +--changes--+                         +--> RETIRED
```

- Ingestion pulls articles from knowledge connectors. A structured article is parsed; a prose article goes to the advisor to produce a draft, with the source text kept alongside for the reviewer.
- A draft never executes. The matcher reads only from published versions, enforced by a database view that the engine role is restricted to.
- Review shows the source article, the structured steps, validator results, the capabilities and tiers involved, and a diff from the previous published version.
- Approval requires a different identity from the author (database constraint and API check).
- Publication computes a SHA-256 over the canonical JSON, assigns a version number, and makes the row immutable. Every run records SOP id, version and hash.
- A model-generated draft is marked as such in the audit log, and the approving human is the accountable author of record for change management purposes.

## 6. Actions

### Tiers

| Tier | Meaning | Examples |
|---|---|---|
| T0 | Read | `autorep`, Splunk search, queue depth |
| T1 | Low risk, reversible, narrow blast radius | Force start of an allow-listed non-critical job; clear an allow-listed temp directory |
| T2 | Production impacting | Force start of a job in a critical box; restart of a service |
| T3 | Prohibited | Anything not expressible as a declared capability; job definition changes; deletes |

A capability's manifest sets its base tier. Tenant policy can raise the tier for specific targets (for example critical jobs) but never lower it below the manifest value.

### Policy engine

Input: tenant, SOP id and version, step, capability, rendered parameters, environment, configuration item, time, change freeze calendar, rate counters, autonomy settings, kill switch state.

Output: one of deny, shadow, recommend, require approval, allow; with a reason code and the rule that decided. The decision is recorded in the audit log before anything else happens. Rules are data owned by the tenant administrator, evaluated by code. Evaluation order is fixed: kill switch, T3, runner ceiling, freeze calendar, time window, allow lists, rate limits, autonomy level. The first deny wins. There is no input from the model.

### Autonomy

| Level | Behaviour |
|---|---|
| Shadow | Record what would be done. Default for new tenants and new SOP versions. |
| Recommend | Post the proposed action to the ticket and escalate. |
| Act after approval | Request approval, execute on approval. |
| Act automatically | Execute without approval, for allow-listed SOP, capability and target combinations only. Capped at T1 unless decision D5 says otherwise. |

Effective autonomy is the minimum of the tenant setting, the SOP setting and the runner ceiling.

### Approvals

An approval request carries the capability, the rendered parameters, the SHA-256 of both, the SOP version hash, the incident, and an expiry. The approver sees exactly those values. The approval record stores the approver's identity from the identity provider and the hash. At execution the engine recomputes the hash; any difference invalidates the approval. For T2 the approver must not be the SOP version's author. Approval channels: the product UI, and ServiceNow approval on the linked change request, polled or received by signed webhook and matched by hash.

### Pipeline

1. Pre-checks: the capability's declared read checks plus any in the SOP.
2. Dry run where the capability supports one.
3. Mint grant: signed, single use, short-lived, bound to the hash.
4. Execute through the runner, which verifies the grant and its own ceiling.
5. Verify: the capability's verify operation with the SOP's assertion, with bounded waits.
6. On failed verification, roll back if declared, then escalate.

### Change management linkage

Every write is linked to a ServiceNow change record: a standard change created from the template named in the SOP, or an existing approved normal change supplied on the ticket. The change number is in the audit event and the work note. If no change record can be created or found, policy denies the write.

### Kill switch

A global flag and a per-tenant flag, checked by the policy engine, again by the worker immediately before execute, and again by the runner against its local flag. Setting it is a single action for tenant admin (tenant) and platform admin (global), is audited, and takes effect within the flag cache lifetime, which is at most a few seconds. In-flight runs move to ABORTED before their next write.

### Autosys force start, reference implementation

Capability `autosys.job.force_start`, tier T1 by default, raised to T2 by tenant policy for listed jobs.

| Stage | Detail |
|---|---|
| Parameter | `job_name`: pattern-restricted string, checked against the tenant's job allow list (exact names or anchored patterns set by the tenant admin). Empty list denies. |
| Invocation | `sendevent -E FORCE_STARTJOB -J <job_name>` as an argument vector through `create_subprocess_exec`, never a shell string; or the Autosys REST API where the tenant configures it. The executable path comes from connector configuration, not from the SOP or the ticket. |
| Pre-checks | `autorep -J` shows the job in FAILURE or TERMINATED; it is not RUNNING or STARTING; no force start has been recorded for this incident (unique constraint); the box, if any, is not on hold. |
| Dry run | Not supported by `sendevent`. The manifest says so; the pre-checks carry the weight. |
| Verify | Poll `autorep -J` for a declared period: pass when status is SUCCESS, or RUNNING followed by SUCCESS. A second FAILURE is a failed verification. |
| Rollback | None declared: a started job cannot be un-started. The manifest records this reason, which is part of why the default tier is not T0 and why critical jobs are T2. |
| On failure | Escalate with both `autorep` outputs and the Splunk and log evidence gathered earlier. |

## 7. Model use

- Provider interface with implementations for an OpenAI-compatible endpoint and Anthropic. Per-tenant allow list of providers, region and retention flags. Requests to a provider not on the tenant's list are refused in code.
- Outbound requests pass through redaction first: secrets by pattern and entropy, personal data by pattern and by ITSM field classification (caller name, email, phone are never sent). Redaction is reversible inside the run through placeholders, so drafted notes can be re-hydrated locally.
- Ticket text and tool output are placed in clearly delimited data sections, with instructions stating they are data. This is a mitigation, not the control. The control is that model output is only ever parsed against a schema into one of the four advisor outputs.
- Budgets: tokens and cost per incident and per tenant per day. Exceeding a budget disables the advisor for that run.
- Each prompt template has a version; the version and digests go to the audit log.
- Evaluation suite: golden incidents with expected terminal state and expected capability calls; an injection corpus with hostile text in ticket fields and in mock tool outputs. The measured property is capability calls at the connector boundary. It runs in CI against the deterministic provider, and on demand against real providers.

## 8. Security architecture summary

| Area | Design |
|---|---|
| Authentication | OIDC and SAML for users; hashed scoped tokens for machines; HMAC with timestamp and nonce for webhooks |
| Authorisation | Seven roles mapped to a fixed permission table; route registry fails start-up on an undeclared route; segregation of duties checked on identity |
| Tenant isolation | PostgreSQL RLS with a non-owner application role; tenant context set per transaction; cross-tenant test suite |
| Web | CSRF tokens, strict CORS from tenant configuration, security headers including a content security policy, rate and body size limits |
| Secrets | References to Vault, AWS Secrets Manager, Azure Key Vault or Kubernetes secrets; resolved per call with a short cache; never in the environment or database |
| Transport | TLS everywhere; mutual TLS between runner and control plane; verification cannot be disabled outside demo mode |
| At rest | Database and backups encrypted by the platform; evidence store encrypted with a per-tenant key |
| Supply chain | Hash-pinned dependencies, SBOM, secret scanning, static analysis, dependency and image scanning in CI, signed images, non-root with read-only root filesystem |
| Logging | Operational logs redact message text and never contain model output or secrets; audit is separate |

## 9. Demo mode

Demo mode remains a first-class configuration at every phase: mock connectors, the deterministic model provider, SQLite, a built-in demo identity with fixed users for each role, and the sample SOPs pre-published. Start-up prints that it is demo mode and refuses to run in demo mode if any real connector credential reference is configured. Every relaxation that demo mode allows (SQLite, local sign-in, TLS options) is refused outside it.

## 10. Open design points

These are engineering questions to settle in the phase where they arise, listed so they are not forgotten.

1. Whether the ITSM connector runs in the control plane or the runner in the hosted shape; depends on whether the customer's ServiceNow is reachable from our cloud.
2. Queue library choice in ADR 0004.
3. The browser-check connector's sandboxing model.
4. Whether personal data can be kept entirely out of the audit chain (ADR 0007), which needs privacy review.
5. How the hosted control plane would hold per-tenant encryption keys (customer-managed keys are the likely requirement).

## 11. Where the product differs from this document after Phase 6

Kept short. Each line names where the as-built position is recorded.

| This document says | As built |
|---|---|
| Mutual TLS between runner and control plane | TLS with the server authenticated by certificate, and every request, call and grant signed at the message level. No client certificates ([ADR 0010](adr/0010-runner-protocol.md)) |
| ITSM and knowledge connectors run in whichever plane can reach the target | Always the control plane ([ADR 0010](adr/0010-runner-protocol.md)) |
| Evidence store (write-once), SIEM export, retention | Not built (controls matrix AU-04, AU-05) |
| Kill switch read through a short cache | Read from the database every time, with no cache |
| Dry run in the action pipeline | Supported by the pipeline; the one shipped write has none |
| Secrets from Azure Key Vault and Kubernetes secrets | Vault, AWS, files (which is how a Kubernetes Secret arrives) and environment; no Azure backend |
| Connectors register through entry points with a signed list of permitted versions | A first-party list in code; no plug-in mechanism ([ADR 0005](adr/0005-connector-sdk.md)) |
| `advisor/`, `tenancy/`, `queue/`, `events/` packages | `ai/`, and the others folded into `db/` and `service/` ([ARCHITECTURE.md](../ARCHITECTURE.md)) |
| A demo identity with fixed users for each role | One demo user holding every role; the tests create one session per role |
| Per-tenant encryption key for evidence | Not built; encryption at rest is the platform's (controls matrix CR-04) |
