# L1 Engineer Agent: architecture as built

This describes the code as it stands after Phase 3 of the [delivery plan](docs/delivery-plan.md). Where the product is going is in [docs/target-architecture.md](docs/target-architecture.md); the reasons for the main choices are in [docs/adr/](docs/adr/).

What has run against a real system: the ServiceNow connector with basic authentication, including paging, the claim step and hand-over to another group (one developer instance), the model client (one hosted provider), the database layer (PostgreSQL 16 in a container) and the Git source (local repositories). ServiceNow OAuth, and every operations connector, have only run against mocks and against fixtures written by hand from vendor documentation. Sign-in has only run against identity providers faked in the tests.

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

The engine is handed the tenant's published SOP versions for each run, read through the `published_sops` view (section 10). `engine/sop_matcher.py` scores each published, valid SOP on keywords (0.50), CI (0.20), category (0.15) and assignment group (0.15). A signal is scored only when the ticket carries it and the SOP declares it; otherwise it is left out and the other weights are rescaled. The run proceeds only if the best score reaches `AGENT_SOP_CONFIDENCE_THRESHOLD` (default 0.6).

An SOP is a JSON document of typed parameters and a graph of steps. Parameters are bound and validated before any step runs. Each check step calls one connector capability, named directly (`CHECK` with `capability`) or through one of the older typed step names; its parameters are validated against the closed schema in the connector's manifest before anything is called (section 9). DECISION and VERIFY steps evaluate a small expression language (`engine/expressions.py`); nothing is passed to `eval`.

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

## 9. Connectors ([ADR 0005](docs/adr/0005-connector-sdk.md))

Three families share one set of rules: ITSM (ServiceNow), knowledge (ServiceNow knowledge base, Git) and operations (Splunk, Dynatrace, IBM MQ, Autosys, file share, browser, mainframe).

- **Manifest.** Each connector declares, as data, its settings, its credential slots and its capabilities: id, read or write, risk tier, a closed parameter schema, required scopes and which parameters are held to an allow list. `connectors/sdk/manifest.py` lints it. The registry (`connectors/sdk/registry.py`) lints every manifest when it is built and does not exist if one fails, so a write capability without a tier, verify operation, closed schema, scopes, rollback statement or allow list for its target cannot ship. Only the first-party list in `connectors/catalog.py` is loaded.
- **One way in.** `Connector.invoke` checks that the capability is declared, that a step context was supplied, that the parameters fit the schema and that every allow-listed parameter has a non-empty list to be checked against, before any connector code runs. A write capability is refused unless the call carries a grant and the connector has been given a verifier to redeem it with (section 11). One write capability is declared: `autosys.job.force_start`.
- **Credentials are references.** A connection holds `vault:`, `aws:`, `file:` or `env:` references (`connectors/sdk/secrets.py`). Values are read when a call needs them, cached for 30 seconds, and never written to the database, the environment or a log. When a call fails and the backend now holds a different value, the connector reads it and repeats the call once, so a rotation takes effect without a restart.
- **HTTP.** `connectors/sdk/http.py` is used by the ServiceNow connectors: TLS verification on and https required outside demo mode, a timeout on every call, one re-read of the credential on `401`, and retries on `429`, `502`, `503` and `504` for reads only.
- **Per tenant.** `connectors/hub.py` builds each tenant's operations connectors from its `connections` rows, falling back to the deployment's environment settings, and re-reads them every few seconds. One bad connection does not take the others down. The ServiceNow ITSM connection and the identity provider are still per deployment.
- **The seven operations connectors** are the original adapters behind manifests (`connectors/ops/` over `adapters/`). Their own HTTP and parsing code is unchanged except for the defects found while writing the contract tests: Dynatrace host checks now follow paging and ask about every host, not the first 20; host names cannot break out of a Dynatrace selector; queue names cannot change the IR360 request path; and the Autosys status is read from the status column of real `autorep` output.
- **ServiceNow.** Basic or OAuth 2.0 authentication, paged lists, and a claim step: the agent assigns the ticket to itself before working it and leaves alone a ticket someone else holds. If the claim cannot be attempted at all, the job is retried later; nothing is guessed. Handing a claimed ticket to another group clears the assignment in the same update, which a stock instance requires. The end of a paged list is taken from `X-Total-Count`, because a page can come back short or empty when access controls hide rows.

## 10. SOP versions

```
draft ──submit──▶ in_review ──approve──▶ approved ──publish──▶ published ──▶ superseded | retired
             ◀──changes requested──
```

`sop/store.py` holds the lifecycle; each transition writes its audit event in the same transaction.

- **Only published versions run.** The engine reads SOPs through the `published_sops` view. A draft, a version in review, an approved-but-unpublished version and a retired one are not in it.
- **Two people.** Submitting fixes the SHA-256 of the canonical content. Approval is of that hash, and the approver is not the author: the service checks it, and a table constraint (`approver_id <> author_id`) holds whatever the code does. A second constraint means no row can be approved, published or retired without an approver and a hash.
- **Immutable.** On PostgreSQL a trigger refuses any change to a submitted version's content, hash or author, any change to an approved version's approver or number, and deletion of anything but a draft, for every role including the table owner. SQLite (demo) has the constraints but not the trigger.
- **Runs record what they ran.** `runs` stores the SOP id, version number and hash. A run interrupted while a newer version is published finishes on the version it started with; if that version was retired, it stops as `needs_human`.
- **Ingestion.** `sop/ingest.py` turns each changed article from a knowledge source into a draft, once per revision. A structured article keeps its steps; a prose article becomes a draft with no steps and the article text, stripped of markup, beside it. Nothing ingested can run until two people have acted on it.

## 11. Actions ([ADR 0008](docs/adr/0008-actions-policy-and-approvals.md))

An `ACTION` step is the only way an SOP changes anything. The engine hands it to `actions/service.py → ActionService.perform` and applies the route it reports to the run state machine.

```
ACTION step
  │
  ├─ decide            actions/policy.py → evaluate: kill switch, ceiling, freeze, hours,
  │                    connector allow list, rate limit, autonomy. First refusal wins.
  │                    Recorded in `actions` and the audit log before anything else.
  │     deny → ESCALATED (kill switch → ABORTED)      shadow → step skipped, run goes on
  │     recommend → note on the ticket, ESCALATED
  ├─ change record     a standard change from the template the SOP names; none → deny
  ├─ approval          if required: run parked (AWAITING_APPROVAL); UI or ServiceNow; expiry
  ├─ pre-checks        the reads the manifest declares, each with an assertion
  ├─ dry run           where the capability has one
  ├─ authorise         one UPDATE: approved|allowed → executing, kill switches read in the
  │                    same transaction. Only then is a grant minted.
  ├─ execute           Connector.invoke redeems the grant, then writes. Never repeated.
  ├─ verify            the manifest's verify read, polled within declared bounds
  └─ roll back         if verification fails and a rollback is declared; then ESCALATED
```

- **Policy is a pure function** over the request and the tenant's rules (`policies.rules`), with no model input. A tenant with no policy is in shadow. Stored rules that no longer parse are read as shadow, not as "no rules".
- **Tiers.** The manifest sets a capability's tier. A tenant rule can raise it to T2 for named targets; no rule can lower one. T2 is never allowed without an approval: the policy function says so and a table constraint (`verdict <> 'allow' OR tier = 'T1'`) holds whatever the code does.
- **The approval is for one action.** The hash covers tenant, incident, SOP version hash, capability and parameters. The approver submits the hash they saw; the engine recomputes it when the run resumes; a table constraint requires `approved_hash = action_hash` on an approved row. For T2 the approver is not the SOP author (service check, table constraint, and by email for a ServiceNow approver). On PostgreSQL a trigger makes a row's capability, parameters, hash, tier and author unchangeable, lets a verdict change only to a denial, and forbids deletion.
- **Grants.** `actions/grants.py → GrantAuthority` signs a grant with a key that exists only in the process. It names tenant, run, step, capability and a digest of the parameters, lasts 60 seconds and is redeemed once. `mint` is called from two methods of `ActionService`, each after the database has recorded the authorisation; a test searches the source tree for any other caller.
- **Kill switch.** Tenant (`policies.kill_switch`), global (`platform_flags`, written only by the migration role) and local (a file). Read from the database every time, at the decision, in the authorising transaction, and when the connector redeems the grant.
- **Suspended runs.** A run waiting for approval has no outcome; its job is `waiting` and is not claimed. A decision or the expiry puts it back on the queue. The worker's sweep expires requests and collects approvals made on the change record in ServiceNow.
- **Interruption.** The `actions` row is the step's journal. Found in `executing`, the write may or may not have happened, so it is never sent again: the engine reads first, and the run goes on only if the target shows the result; otherwise it ends `needs_human`. Found in `executed`, only the verification is repeated. A unique index allows one execution of the same call per incident.
- **Autosys force start.** `sendevent -E FORCE_STARTJOB -J <job>` as an argument list, from `connectors/ops/autosys.py`. The read-only adapter is unchanged and still cannot run it. The job must match the connection's `force_start_jobs` list, be in `FA` or `TE` at the pre-check, and read `SU` at verification; a second `FA` or `TE` fails it at once. No rollback is declared, because a started job cannot be un-started.

The force start has only run against a mock scheduler: the real `sendevent` command, its exit codes and its timing are unverified. ServiceNow change records and approvals are written from vendor documentation and have not run against an instance.

## 12. Other safeguards

- **Model boundary.** Ticket text is redacted, length-limited and delimited before it is sent. Model output is never logged and is only ever read as an SOP choice.
- **Logs and secrets.** Operational logs are JSON with pattern-based redaction. Secrets fetched from Vault or AWS are passed to the settings object and are not written to the process environment.

## 13. What this design does not have yet

| Gap | Consequence today | Phase |
|---|---|---|
| ITSM and identity provider are per deployment | One process serves one tenant's ServiceNow and identity provider. Operations connectors are per tenant. | 6 |
| Connectors still run through the original adapters | Read retries are not limited to the error classes a capability lists as retryable. Writes are never retried. | 6 |
| Connectors are not loaded through entry points, and there is no signed list of permitted connector versions | Only the first-party list in code is loaded. | 6 |
| The engine role is not restricted to the published view | The view is the only path the engine code uses; the application role can still read the version table. | 6 |
| Confluence and SharePoint sources; the `lookup` parameter source; drafting steps from prose | Prose articles become empty drafts for a person to structure. | 5 |
| No screen for connections | Command line and API only. | 6 |
| The browser connector uses a local Chrome | Marked preview and not in the default image. | 6 |
| One write capability, verified only against a mock | The Autosys force start has not run against a scheduler. The Autosys REST API is not implemented. | needs a test environment |
| ServiceNow change and approval calls are unverified | Written from documentation. An existing normal change on the ticket is not accepted; approvals are polled, with no signed webhook. | needs an instance |
| A grant is checked in the process that minted it | Splitting the runner out needs an asymmetric key pair. | 6 |
| Policy rules have no editing screen; no per-run stop | Rules are JSON through the API or CLI. A waiting run is stopped by the kill switch or by its request expiring. | 6 |
| Runner not split out | Adapters run in the worker process, not behind the outbound-only runner protocol of ADR 0003. | 6 |
| Audit export, retention, legal hold, evidence store | The chain stays in the database; checkpoints are stored by hand. | later phases |
| Rate limit is per process | Behind several replicas the limit multiplies. | 6 |
| A crash during the final ServiceNow update | The closing work notes can be posted twice. Not fixed in Phase 4 either. | 6 |
| No model evaluation suite | Redaction and injection resistance are unit tested, not measured on a corpus. | 5 |
