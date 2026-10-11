# L1 Engineer Agent: architecture as built

This describes the code as it stands after Phase 2 of the [delivery plan](docs/delivery-plan.md). Where the product is going is in [docs/target-architecture.md](docs/target-architecture.md); the reasons for the main choices are in [docs/adr/](docs/adr/).

What has run against a real system: the ServiceNow client (one developer instance), the model client (one hosted provider), and the database layer (PostgreSQL 16 in a container). Every tool adapter has only run against its mock, and sign-in has only run against identity providers faked in the tests.

## 1. Shape

```
ServiceNow ──signed webhook──▶ API process ─┐
ServiceNow ◀──poll (New, one group)── poller ┤
                                             ▼
                         intake: one transaction
                         incident + run + job + audit event     ◀── unique constraint:
                                             │                      one run per ticket
                                             ▼
                                   PostgreSQL (jobs table)
                                             │  claim with a lease (SKIP LOCKED)
                                             ▼
                                         worker(s)
                         IncidentProcessor ─▶ SOPMatcher ─▶ SOPExecutor
                                             │                 │
                                journal: SOP choice and        └─▶ adapters (read only)
                                each step result, committed
                                with its audit event
                                             ▼
                         outcome ─▶ ServiceNow update ─▶ run record + audit event
                                             │
                    events table ─▶ server-sent events ─▶ React dashboard
```

Processes: `l1-agent serve` runs the HTTP listener, the poller and, by default, workers in the same process. `l1-agent worker` runs workers alone, so they can be scaled and restarted separately. All state is in the database; a process can be killed at any point.

## 2. One engine

There is one execution path and it is deterministic ([ADR 0001](docs/adr/0001-one-execution-engine.md)). A model never chooses a tool, a step or an argument. If a model is configured it is asked one question: which of the candidate SOPs fits this ticket, or none. Its answer is used only if the SOP it names already clears the confidence threshold on the matcher's own score.

## 3. Outcomes

Every run ends in exactly one of four outcomes ([ADR 0002](docs/adr/0002-fail-closed-outcomes.md)).

| Outcome | Reached when |
|---|---|
| `resolved` | A VERIFY step passed, the SOP then reached a RESOLVE step, and ServiceNow accepted the Resolved update. |
| `escalated` | The SOP reached an ESCALATE step, a check failed with no other route, no SOP matched, or the match was below the threshold. |
| `needs_human` | The engine could not continue safely: invalid SOP, unbound parameter, missing adapter, open circuit breaker, internal error, ServiceNow refused the Resolved update, or the run kept dying and was stopped. |
| `aborted` | Reserved for an operator stop. Nothing triggers it yet. |

Three independent layers stop an unverified resolution: the SOP validator (`sop/validator.py`), the run state machine (`engine/state_machine.py`, a data table in which `RESOLVED` is reachable only from `VERIFIED`), and the summary type (`models/evidence.py`), which raises if `RESOLVED` is set without the step that verified it. `tests/unit/engine/test_no_unverified_resolve.py` searches for a route around them.

## 4. SOP matching and execution

`engine/sop_matcher.py` scores each published, valid SOP on keywords (0.50), CI (0.20), category (0.15) and assignment group (0.15). A signal is scored only when the ticket carries it and the SOP declares it; otherwise it is left out and the other weights are rescaled. The run proceeds only if the best score reaches `AGENT_SOP_CONFIDENCE_THRESHOLD` (default 0.6).

An SOP is a JSON document of typed parameters and a graph of steps. Parameters are bound and validated before any step runs. Each check step calls one adapter, with its parameters validated against a closed schema (`adapters/schemas.py`). DECISION and VERIFY steps evaluate a small expression language (`engine/expressions.py`); nothing is passed to `eval`.

## 5. Durable runs ([ADR 0004](docs/adr/0004-persistence-and-queue.md))

- **One run per ticket.** Intake inserts the incident with `ON CONFLICT DO NOTHING` on `(tenant, source, external id)`. Whoever inserts the row creates the run and its job in the same transaction; a repeat delivery creates nothing. A partial unique index allows one open run per incident.
- **Queue.** A `jobs` table. A worker claims a job by taking a lease with `SELECT ... FOR UPDATE SKIP LOCKED` and renews it while the run makes progress. A lease that is not renewed expires, and another worker takes the job.
- **Resume.** The engine writes a journal: the SOP it chose, and each step's result as it completes, each committed with its audit event. A resumed run reuses the recorded SOP choice and replays recorded step results without calling the target systems again. The route through an SOP depends only on step results, so replay reaches the point of interruption. Only the step that was in flight is executed again.
- **Giving up.** A run that does not finish within `WORKER_MAX_ATTEMPTS` is recorded as `needs_human` and routed to a team.
- **Events.** Progress events are rows in an `events` table, so the dashboard stream works whichever process did the work.

Demo mode uses the same code on a throwaway SQLite file. SQLite is refused outside demo mode.

## 6. Tenancy ([ADR 0003](docs/adr/0003-deployment-shape.md))

The lead deployment is one tenant in the customer's environment. Isolation is enforced as if there were many, so that a hosted, multi-tenant shape is a deployment change and not a rewrite.

- Every table except `tenants` carries `tenant_id`. PostgreSQL row level security is enabled and forced on each, with a policy that compares `tenant_id` to a setting on the transaction.
- All tenant data is reached through `Database.tenant(tenant_id)`, which sets that value. A query that forgets its own filter still sees one tenant.
- The application connects as a role that does not own the tables, cannot bypass row level security, cannot create a tenant and cannot change audit rows. At start-up the service checks its own role and refuses to run otherwise. Migrations and tenant creation use a separate role.
- A credential carries its tenant (`<tenant>.<secret>` for a session, `l1t_<tenant>_<secret>` for a token) and is looked up inside that tenant only.

What is still per deployment and not per tenant: the ServiceNow connection, the webhook secret, the identity provider, the tool adapters and the L2 routing file. One process therefore takes incidents for one tenant. That changes in Phase 3 with the connector registry.

## 7. Identity and access ([ADR 0006](docs/adr/0006-http-framework-and-identity.md))

- **Deny by default.** Every route is added through one registry (`api/access.py`) with a declaration: public with a stated reason, webhook signature, any signed-in principal, or a named permission. Building the application walks the route table and fails if anything is undeclared, including framework documentation routes. A check before every handler applies the declaration.
- **Sign-in.** OpenID Connect (authorisation code with PKCE; ID token verified against the provider's keys, asymmetric algorithms only) and SAML 2.0 (signed assertions, answers to our own requests only). There are no local user passwords.
- **Roles.** Viewer, operator, approver, SOP author, SOP approver, tenant administrator, auditor. A user gets roles only through a mapping from identity provider groups; no mapped group means no sign-in. Routes name permissions; the role-to-permission table is fixed in code. An administrator manages access but does not read the audit log or approve.
- **Sessions.** Server-side, in the database, with an absolute and an idle lifetime. The cookie is HttpOnly, Secure and SameSite=Lax; only its hash is stored. State-changing requests need a CSRF token.
- **Machine callers.** Scoped API tokens: hashed, expiring, revocable, and limited to read permissions.
- **Webhook.** HMAC over timestamp and body, a five-minute window, and a replay record in the database.
- **Break-glass.** One local administrator, disabled unless a password hash is configured, one-hour sessions, every attempt audited.
- **Limits.** Request body size, a per-client rate limit (tighter on sign-in), listed-origin CORS, security headers on every response.

## 8. Audit log ([ADR 0007](docs/adr/0007-audit-log.md))

`audit_events` is one hash chain per tenant. Each event holds a sequence number, the actor, a correlation id (the run), a type, a canonical JSON payload, the previous event's hash and its own. An event is appended in the same transaction as the change it describes.

- The application role can insert and read. Update, delete and truncate are revoked, and blocked by a trigger for every role including the owner.
- `l1-agent audit verify` recomputes the chain and reports a changed, removed, inserted or reordered event.
- A chain cannot show that its newest events were removed or that the whole chain was rewritten. `l1-agent audit checkpoint` signs the head of the chain; kept outside the database, checkpoints make both detectable.
- One run's timeline can be rebuilt from audit events alone (`l1-agent audit timeline --run`).
- Free text is redacted and bounded before it enters the chain. Evidence is recorded as a digest.

Recorded today: incident intake, SOP choice, each step start and result, the outcome, sign-in success and failure, sign-out, break-glass use, token and role-mapping changes, and refused access.

## 9. Other safeguards

- **Adapters.** All read only. Each validates inputs against an allow list, and an empty allow list denies. TLS verification cannot be turned off outside demo mode.
- **Model boundary.** Ticket text is redacted, length-limited and delimited before it is sent. Model output is never logged and is only ever read as an SOP choice.
- **Logs and secrets.** Operational logs are JSON with pattern-based redaction. Secrets fetched from Vault or AWS are passed to the settings object and are not written to the process environment.

## 10. What this design does not have yet

| Gap | Consequence today | Phase |
|---|---|---|
| Connections are per deployment | One process serves one tenant's ServiceNow and identity provider. | 3 |
| No connector framework; basic authentication to ServiceNow | Adapters are hard-wired; no OAuth, pagination or ticket claim step. | 3 |
| No SOP review workflow | SOPs are files or knowledge articles; nothing records who approved one. The demo HTML editor was removed. | 3 |
| No write actions, policy engine or approvals | The agent diagnoses and verifies; it does not remediate. | 4 |
| Runner not split out | Adapters run in the worker process, not behind the outbound-only runner protocol of ADR 0003. | 6 |
| Audit export, retention, legal hold, evidence store | The chain stays in the database; checkpoints are stored by hand. | later phases |
| Rate limit is per process | Behind several replicas the limit multiplies. | 6 |
| A crash during the final ServiceNow update | The closing work notes can be posted twice. | 3 |
| No model evaluation suite | Redaction and injection resistance are unit tested, not measured on a corpus. | 5 |
