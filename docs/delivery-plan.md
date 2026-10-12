# Delivery plan

Status: Phases 1 and 2 delivered (records in sections 5 and 6). Sections 1 to 4 are the Phase 0 plan as written and describe the code before Phase 1.

This plan has three parts: the verified baseline (what the code does today), the decisions that need an owner before Phase 1 starts, and the phase-by-phase scope with exit criteria.

## 1. Verified baseline

### 1.1 Which code was reviewed

The brief cites commit `db38e1f`. That commit is the head of the remote branch `thilak-feature-branch`. It was not present in the local clone and is not merged to `master`.

| Ref | Commit | Relationship |
|---|---|---|
| `origin/thilak-feature-branch` | `db38e1f` | The code the brief describes. 13 commits ahead of the local checkout. Not on `master`. |
| `origin/master` (default branch) | `0d1d159` | Contains the local checkout's commit, does not contain `db38e1f`. |
| Local checkout `feature/dynatrace-memory-sop` | `f50ede2` | Ancestor of both. Has 20 modified and 9 untracked frontend files that are not committed anywhere. |

Every finding below was checked against an export of `db38e1f`. Where the local checkout differs, the difference is noted. Which of these three becomes the Phase 1 base is decision D1 in section 2.

### 1.2 Test results (run, not read)

| Suite | At `db38e1f` | At local `f50ede2` |
|---|---|---|
| `pytest tests/unit` | 180 passed, 2 failed | 180 passed, 2 failed |
| `pytest tests/integration` | 3 passed, 3 failed, 6 errors | same |
| `pytest tests` | collection aborts on `tests/test_nvidia.py` | 183 passed, 5 failed, 6 errors (file absent) |
| `ruff check src tests` | not run | 24 errors |
| `ruff format --check` | not run | 47 files would be reformatted |

The two unit failures are `test_execute_sop_auto_mode_resolves` and `test_execution_summary_has_timing`. Both assert `total_duration_ms > 0` and get `0.0`. The cause is the coarse resolution of `time.monotonic()` on Windows; they are likely to pass on the Linux CI runner. The fix belongs in the timing code (`time.perf_counter`), not in the tests.

### 1.3 Section 2 of the brief, point by point

**Confirmed as written** unless a correction is given.

What exists

| # | Claim | Result |
|---|---|---|
| 1 | Three execution paths chosen by environment variables | Confirmed (`main.py`, `L1AgentService.__init__`). |
| 2 | Seven adapters, all read only; `requires_approval` escalates; no write action | Confirmed. `AutosysAdapter` only runs `autorep` and `job_depends`. |
| 3 | In-memory and JSONL state, one process, one tenant, env credentials | Confirmed. The idempotency set is per process and is lost on restart. |
| 4 | 182 unit tests pass | **Correction:** 182 are collected, 180 pass, 2 fail on this machine (see 1.2). |

Defects that close tickets wrongly. All five were reproduced with a script against `db38e1f`.

| # | Claim | Reproduced result |
|---|---|---|
| 1 | Executor defaults to RESOLVED | Zero-step SOP gives `resolved`. Sample Autosys SOP with a failing Autosys adapter runs `step-2 fail` then `step-escalate success` and gives `resolved`. A DECISION with rule `depth > 1000` takes the success branch and gives `resolved`. |
| 2 | `AIExecutor` has the same default | Confirmed in code (`ai_executor.py:135`). `chat_with_tools` logs a warning at the iteration cap and returns normally, leaving the default. |
| 3 | Duplicate incident returns RESOLVED | Confirmed (`incident_processor.py:92-99`). The CrewAI processor returns FAILED for the same case, so the two paths disagree. |
| 4 | Prose KB article becomes an SOP with no steps | Confirmed: `steps=[]`, and execution gives `resolved`. |
| 5 | "Resolved" means "the checks ran" | Confirmed: the memory scenario resolves while the mock reports memory at 92.5 percent. |

Defects that break real integrations

| # | Claim | Result |
|---|---|---|
| 1 | `sysparm_display_value=all` breaks polling | Confirmed: `sys_id` arrives as a `dict` and `sys_id in set()` raises `TypeError: unhashable type: 'dict'`. `number`, `short_description`, `description`, `category`, `subcategory` and `sys_created_on` are also left as dicts. |
| 2 | `${job_name}` is never substituted | Confirmed. The real adapter's job-name pattern rejects the literal string. The placeholders appear only in `autosys_job_failure.json`. |
| 3 | CrewAI path | Confirmed on every point. `post_work_note` and `l2_router.route` do not exist; both calls are inside `try/except` that logs a warning. `step.step_type.value` fails because `step_type` is a `str`. The outcome comes from a regex over free text, and the fallback reads RESOLVED from a bare keyword in the last 500 characters. The `crewai==0.11.2` wheel exports only `Agent`, `Crew`, `Process`, `Task`, and its `crewai/tools/__init__.py` is empty. **Difference:** the local checkout pins `crewai>=0.80.0` instead and the installed version is 1.14.5, so the import errors apply to `db38e1f` only. |
| 4 | AI mode uses the keyword matcher | Confirmed. `select_sop` and `analyze_incident` are called only from `demo.py`. |

Security gaps

| # | Claim | Result |
|---|---|---|
| 1 | No authentication, open CORS | Confirmed. No auth on any of the 13 routes. The webhook has no signature check. |
| 2 | SOP editor | Confirmed: f-string HTML with no escaping, no CSRF token, no schema validation, no version or author record. Save overwrites the file; delete unlinks it. The SOP id is restricted to a safe pattern, so there is no path traversal. |
| 3 | Tool restriction exists only in the prompt | Confirmed. `get_investigation_tools()` returns every tool on every run. Adapter arguments from the model are passed to `adapter.execute()` with no schema validation, so undeclared keys also pass through. |
| 4 | Untrusted text to an external model, no redaction | Confirmed. |
| 5 | Adapter issues | Confirmed, with one **correction**: the Windows share adapter already denies when the allow list is empty. The prefix match and caller-supplied regex are as described. |
| 6 | Logging and secrets | Confirmed. Model reasoning and tool arguments are logged at INFO (`llm_client.py:96-107`). Secrets are written to `os.environ`. |

Hygiene

| # | Claim | Result |
|---|---|---|
| 1 | Integration tests and `test_nvidia.py` | **Correction:** there are 12 integration tests; 9 fail or error on `SOPParser.parse`, 3 pass. `test_nvidia.py` is confirmed as a PowerShell here-string that aborts collection. |
| 2 | CI, requirements, `.env.example` | Confirmed at `db38e1f`. **Difference:** `.env.example` exists and is tracked on the local checkout and on `master`; `db38e1f` deleted it. |
| 3 | Customer name, logo, email addresses | Confirmed in 15 files at `db38e1f`. See 1.4 for two points the brief does not mention. |

### 1.4 Additional findings not in the brief

| # | Finding | Why it matters |
|---|---|---|
| A1 | The customer name and logo are in git history on the remote, on every branch. | Removing them from the tree does not remove them from history. Rewriting published history is destructive and needs an explicit decision (D2). |
| A2 | The web UI adapter skips the allow-list check when the URL has no hostname. | `file:///...` and similar URLs pass even with a populated allow list. There is no scheme check. |
| A3 | Webhook and poll processing use `asyncio.create_task` with no reference kept and no persistence. | An accepted incident is lost on restart, and the webhook has already returned 200. |
| A4 | In the CrewAI path a FAILED outcome publishes `incident_escalated` but changes nothing in ServiceNow and routes nowhere. | The dashboard shows an escalation that did not happen. |
| A5 | Model-supplied `screen_navigation` is not in the declared tool schema but is accepted by the mainframe adapter because arguments are not validated. | The model can send arbitrary keystrokes to a mainframe session. |
| A6 | Existing unit tests assert the defective behaviour: five tests in `test_executor.py` and one in `test_ai_executor.py` expect RESOLVED after a single read step with no verification. | Fixing the defect changes these expectations. This is a correction of the specification, and each change will be listed for review (see Phase 1). |
| A7 | `pyproject.toml` declares the licence as MIT. | Not appropriate for a commercial product unless intended (D7). |
| A8 | The Dockerfile already creates and uses a non-root user. | Partial credit toward the container requirement; read-only filesystem, pinned base digest and signing are missing. |
| A9 | `IR360Settings` has no `verify_ssl` field at all; Splunk and Dynatrace can disable verification through an environment variable in any mode. | The requirement is that verification cannot be disabled outside demo mode. |
| A10 | CI security scan runs `bandit ... || true` for medium severity and the workflow never triggers on `master`. | No security gate has ever run. |

## 2. Decisions needed before Phase 1

These are product, commercial or irreversible choices. Each has a recommendation; Phase 1 can start once D1 to D3 are answered.

| # | Decision | Recommendation |
|---|---|---|
| D1 | Phase 1 base branch. Options: `db38e1f` (the reviewed code, unmerged), `master`, or the local checkout with its uncommitted frontend redesign. | Base on `master`, merge `thilak-feature-branch` backend changes that survive ADR 0001, and commit the local frontend redesign as its own change first so it is not lost. Five frontend files are changed on both sides (`App.jsx`, `ActivityFeed.jsx`, `IncidentDetail.jsx`, `LiveDashboard.jsx`, `NavBar.jsx`) and someone who knows both must pick. |
| D2 | Customer name in git history on a hosted remote. | Remove from the tree in Phase 1. Separately decide whether to rewrite history or start the product in a fresh repository. A fresh repository is cleaner for a commercial product and avoids force-pushing. |
| D3 | CrewAI path: repair it in Phase 1, or remove it in Phase 1. | Remove. ADR 0001 retires it, it cannot be verified without a provider key, and repairing code that is scheduled for deletion is wasted review effort. If you want it kept alive until Phase 5, Phase 1 repairs the four defects and forces its outcome through the same fail-closed mapping. |
| D4 | Lead deployment shape (ADR 0003). | Single-tenant in the customer environment first; keep the runner split so the hosted control plane can follow. |
| D5 | Highest tier allowed to run without a human approval. **Decided: as recommended.** | T1 only. T2 always needs an approval bound to the action hash, in every autonomy level. This is a liability position as much as an engineering one. |
| D6 | Model providers at launch, and whether "no model" is a supported configuration. **Decided: as recommended.** | Support a customer-supplied OpenAI-compatible endpoint and Anthropic. Make the engine fully functional with the model disabled, because some buyers will start that way. |
| D7 | Product name, licence, and whether the mainframe and browser connectors ship in the first release. **Decided before Phase 6:** the name is "L1 and L2 AI-Ops"; no licence is declared for now; both connectors ship as recommended. | Ship both connectors as T0 read only and labelled preview. They carry the most risk for the least reference-case value. |
| D8 | ServiceNow approval mechanism: approval on a change request created from a standard change template, or a custom approval record. Whether we ship a ServiceNow scoped application. **Decided: as recommended.** | Standard change template plus approval on the change request. A scoped application is the durable answer but adds store certification work; defer it to after Phase 6. |

## 3. Phases

Rules that apply to every phase: one branch, one reviewable change set, tests with the change, demo mode works at the end, no weakened tests, nothing pushed unless asked. Every phase summary states what was run, what was not, and that behaviour against real ServiceNow, Autosys, Splunk and Dynatrace is unverified.

### Proposed change to the brief's sequencing

The brief places "single engine" in Phase 5. The fail-closed outcome model in Phase 1 needs explicit terminal states and a verification step, which is the core of the state machine. Building that into three engines and then deleting two is wasted work. The proposal:

- Phase 1 introduces the state machine core in memory and routes the rule-based path through it. The LLM tool loop and CrewAI paths are removed or, if D3 says keep, wrapped so they cannot produce RESOLVED.
- Phase 2 persists the state machine.
- Phase 5 keeps its model-hardening scope (provider abstraction, redaction, evaluation suite, advisor tasks) without the engine consolidation.

### Phase 1: correctness and safety

Branch: `phase-1/correctness-and-safety`

| Work | Detail |
|---|---|
| Outcome model | Terminal states RESOLVED, ESCALATED, NEEDS_HUMAN, ABORTED. No default. New step types VERIFY, ESCALATE, RESOLVE. RESOLVE is reachable only from a passed VERIFY. See ADR 0002. |
| SOP validation | Static validator: at least one step, all references resolve, every failure edge that is not set goes to ESCALATE, no route to RESOLVE without VERIFY. An SOP that fails validation is not loaded. |
| Decision rules | Small typed expression language over prior step outputs. An unrecognised rule is a validation error at load and NEEDS_HUMAN at run time. |
| Sample SOPs | Rewritten to the new schema with VERIFY steps. `step-escalate` becomes an ESCALATE step. |
| Duplicates | A duplicate produces no run and no summary; the caller receives an explicit `duplicate` result. |
| Parser | A prose article produces a draft that cannot execute. Full lifecycle arrives in Phase 3. |
| ServiceNow | Unwrap display values for every field; fixture-based contract tests for `all`, `true` and `false`. |
| Parameters | Minimal typed parameter binding for `job_name` from the ticket, with validation; unbound parameter means NEEDS_HUMAN. Full model in Phase 3. |
| Interim access control | Deny by default. Static bearer tokens from configuration for API routes, HMAC signature with timestamp on the webhook, CORS restricted to configured origins, listener bound to loopback unless configured. The HTML SOP editor is disabled outside demo mode and escapes its output. This is a stopgap until Phase 2. |
| Adapters | TLS verification on and not configurable outside demo mode. Web UI: scheme and exact-host allow list, empty means deny. Windows share: path-segment match and no caller-supplied regex. Mainframe: no model-supplied keystrokes. Dynatrace: an API error is a failed check, not a healthy host. Argument schemas enforced before `execute`. |
| Logging | Redaction applied to message text; model output not logged at INFO; secrets no longer written to `os.environ`. |
| Hygiene | Fix the nine integration tests, remove `test_nvidia.py`, CI on the real default branch, `pytest-timeout`, complete requirements, restore `.env.example`, ruff clean. |
| Branding | Remove the customer name, logos and addresses from code, config, fixtures and docs. Branding and L2 routing move to configuration with neutral defaults. Add a CI check that fails on a deny list of terms kept outside the repository. |

Exit criteria

1. A test enumerates every path through the engine transition table and through each sample SOP and finds no route to RESOLVED without a passed VERIFY.
2. Property-based tests generate random SOP graphs; the validator rejects every graph that has such a route, and the executor never returns RESOLVED for any graph the validator rejects.
3. All tests pass on Linux CI and locally, including the two timing tests.
4. A repository search for the customer name returns nothing in the working tree.
5. Demo mode runs the four demo incidents; the memory scenario ends ESCALATED, not RESOLVED, because memory is still high.

Test expectations that will change, each to be listed in the phase summary: the six tests in A6, the integration test `test_resolves_mq_incident`, and `test_ai_mode_processes_incident`. They currently assert RESOLVED for check-only flows.

### Phase 2: foundations

Branch: `phase-2/foundations`

Scope: PostgreSQL with Alembic migrations; tenant model with row level security; durable job queue on PostgreSQL; idempotency by unique constraint; persisted state machine with transactional outbox; OIDC and SAML sign-in; the seven roles enforced by a deny-by-default route registry including SSE and metrics; scoped API tokens; CSRF, security headers, rate and size limits; the audit log with hash chain and `verify` command; HTTP layer moved to the framework chosen in ADR 0006.

Exit criteria

1. Two worker processes given the same incident: exactly one run is created (test against real PostgreSQL in CI).
2. Cross-tenant tests: with tenant A's session, every repository call and every API route returns nothing or 404 for tenant B's rows, including when the application layer filter is deliberately omitted.
3. A route registered without a declared permission fails application start-up.
4. `audit verify` detects a modified row, a deleted row and a reordered row in tests.
5. Killing a worker mid-run and restarting resumes from the last committed state without repeating a completed step.

### Phase 3: connectors and knowledge

Branch: `phase-3/connectors-and-knowledge`

Scope: connector SDK, manifest and registry; credential references and rotation; ServiceNow ITSM connector with OAuth 2.0, pagination, claim step and signed webhooks; knowledge connectors for ServiceNow KB and Git first, Confluence and SharePoint behind the same interface; the seven adapters ported; SOP lifecycle with draft, review, approval by a different person, immutable hashed versions; typed parameters; React review and approval screen; removal of the HTML editor.

Exit criteria

1. A manifest lint test fails any connector whose write capability lacks a risk tier, verify operation, parameter schema or required scopes.
2. Contract tests replay recorded fixtures for each connector, including error and pagination cases.
3. A draft SOP cannot be selected by the matcher (test at the data layer, not only the API).
4. The same user cannot author and approve one SOP version (test at the data layer by constraint, and at the API).
5. Rotating a credential in the secrets backend takes effect on the next call without restart.

### Phase 4: actions

Branch: `phase-4/actions`

Scope: risk tiers, policy engine, autonomy levels, approvals bound to action and parameter hash, change record linkage, kill switch, action pipeline (pre-checks, dry run, execute, verify, rollback), Autosys force start through the argument list or REST API, approval through ServiceNow and through the UI.

Exit criteria

1. The Autosys scenario passes end to end against the mock in shadow, approval and automatic modes, and a test reconstructs each run from the audit log alone.
2. A structural test proves no connector write capability can be invoked without a policy decision record: the write entry point requires a decision token that only the policy engine can mint.
3. Approval tests: changed parameters invalidate the approval; expired approval is rejected; SOP author cannot approve a T2 action.
4. Kill switch test: with the switch on, an in-flight run stops before execute and a queued write is refused, globally and per tenant.
5. Verification failure triggers rollback where declared, then escalation.

### Phase 5: model hardening

Branch: `phase-5/model-hardening`

Scope: provider abstraction with per-tenant allow list, region and retention settings; the four advisor tasks with schema-validated output; redaction before egress; token and cost budgets; prompt versioning; the evaluation suite with golden incidents and the injection corpus in CI; model inventory and change log documents.

Exit criteria

1. The injection corpus produces zero tool calls outside the current step's declared capability and zero writes, measured at the connector boundary, not from model output.
2. With the model provider disabled or returning garbage, every golden incident ends in a correct terminal state or NEEDS_HUMAN, never RESOLVED without verification.
3. A redaction test corpus shows no seeded secret or personal data item in any outbound provider request.
4. Exceeding the per-incident budget ends the advisor's involvement and the run continues deterministically or goes to NEEDS_HUMAN.

### Phase 6: packaging

Branch: `phase-6/packaging`

Scope: Helm chart; runner image and outbound-only protocol; connector setup wizard; tenant onboarding flow; operator and security documentation; SBOM, image signing, hash-pinned dependencies, scanning in CI; controls matrix completed with every row pointing at code and a passing test.

Exit criteria

1. The "done means" list in section 10 of the brief, executed as an automated end-to-end test against mocks: onboard a tenant, connect ServiceNow, a knowledge source, Autosys and Splunk, approve an SOP, set autonomy to shadow, run the scenario.
2. A CI job parses the controls matrix and fails if any row marked implemented points at a missing file or a test that does not pass.
3. The container runs as non-root with a read-only root filesystem in the chart's default values, verified by a chart test.

## 4. What cannot be verified in this project

There is no live ServiceNow, Autosys, Splunk, Dynatrace, IBM MQ, mainframe, identity provider or SIEM. Everything is built against mocks, recorded fixtures and contract tests written from vendor documentation. The following stay unverified until a customer or sandbox environment exists, and each phase summary will repeat the relevant items:

- ServiceNow OAuth flows, ACL behaviour, business rules that reject updates, and display-value shapes on a customised instance.
- Autosys `sendevent` exit codes and timing, REST API availability by version, and `autorep` output formats across versions.
- Real identity provider behaviour for OIDC and SAML, including group claim formats.
- PostgreSQL row level security under the customer's chosen connection pooler.
- Performance and queue behaviour at production incident volumes.

## 5. Phase 1 record

Delivered on branch `phase-1/correctness-and-safety`, based on `master`.

Decisions applied (the recommendations in section 2 were accepted): D1 base on `master` with the local frontend redesign committed first on `wip/frontend-redesign`; D2 history left untouched; D3 CrewAI removed.

Deviations from the plan above

| Item | What happened |
|---|---|
| `thilak-feature-branch` (`db38e1f`) | Not merged. Its backend changes were NVIDIA endpoint logging and a keyword-matcher override inside the AI path, neither of which survives ADR 0001. Its frontend changes (AI reasoning tab, terminal-style activity feed, a different navigation bar) overlap the redesign in five files and were left for a human to reconcile. |
| Legacy `ARCHITECTURE.md` and `RUNBOOK.md` | Replaced with short documents that describe only what exists after Phase 1. `README.md` was rewritten. |
| SOP matcher scoring | Not in the Phase 1 plan. Changed after a ServiceNow developer instance showed realistic tickets scoring 0.41 to 0.51 against the 0.6 threshold. See the table below. |
| Licence | The MIT declaration was removed from `pyproject.toml`. No licence is declared; D7 stays open for the product name and terms. |
| Container image | Found by building it: the connector requirements named a `py3270` version that was never published, a plain `docker build` produced the demo image because the demo stage was last, and a local build copied run history into the image. All three fixed; a `.dockerignore` was added. |
| Requirements | Split into `requirements.txt` (runtime), `requirements-dev.txt` and `requirements-connectors.txt` instead of one file. |
| Demo mode ServiceNow | Demo mode now uses an in-memory ServiceNow stand-in; before, it called the real client with an empty URL and logged an error per note. |
| Polling in demo mode | Disabled, for the same reason. |

SOP matcher scoring change (threshold unchanged at 0.6)

| What | Before | Now |
|---|---|---|
| Keyword score | Keywords found divided by the number of keywords the SOP lists, so a long list of alternative phrasings lowered every score | Three distinct hits is full score |
| Keyword and title matching | Substring (`ram` matched `program`; `-` and `and` in a title counted) | Whole words; title stopwords and punctuation ignored |
| Signal missing on the ticket or not declared by the SOP | Scored as a mismatch | Left out and the other weights rescaled |
| Signal present and different | Counts against | Counts against (unchanged) |

Effect on a fixed set of tickets: the four matching demo incidents rise from 0.75 to 0.86 to between 0.94 and 1.00; the certificate incident with no SOP falls from 0.11 to 0.00; the two developer-instance tickets rise from 0.51 and 0.41 to 0.81 and 0.74; a ticket with one generic keyword and a matching group scores 0.49; matching fields with no keyword evidence stay at 0.50. A ticket with no CI, category or group is now scored on text alone and can clear the threshold with two or more keyword hits, where before it could not.

Test expectations changed, as announced

| Test | Before | Now | Reason |
|---|---|---|---|
| `test_executor.py`: note, MQ, Splunk, Autosys, file, decision, multi-step | RESOLVED after check-only steps | ESCALATED, through an explicit terminal step | Defect 5: nothing was verified |
| `test_executor.py::test_missing_adapter_*` | ESCALATED | NEEDS_HUMAN | A missing connector is an engine condition, not an SOP routing decision |
| `test_executor.py::test_unknown_step_type_*` | FAILED | NEEDS_HUMAN | FAILED no longer exists; the SOP is rejected before it starts |
| `test_incident_lifecycle.py::test_ai_mode_processes_incident` | RESOLVED, ESCALATED or PARTIAL | ESCALATED | Tightened; PARTIAL no longer exists |
| `test_dynatrace_memory_scenario.py` step count | 8 | 10 | Sample SOP gained VERIFY and explicit terminals |
| `test_confidence_threshold.py`, `test_models.py` | Built RESOLVED summaries with no verification | Supply `verified_by` | The summary type now refuses the old construction |
| `test_analyzer.py` | unchanged | unchanged | The rationale text was reworded to keep the assertion true without echoing model output |

Tests removed with the code they covered: `tests/unit/ai/test_ai_executor.py` (6 tests), eight investigation-tool cases in `test_tool_definitions.py`, one in `test_mock_llm.py` (replaced by a test that the mock makes no operational tool call).

Not done in Phase 1, by design: persistence, tenancy, SSO and roles, audit log, connector SDK, SOP review workflow, write actions, redaction evaluation corpus. See phases 2 to 5.

## 6. Phase 2 record

Delivered on branch `phase-2/foundations`, based on `phase-1/correctness-and-safety`. Decision applied: D4, single tenant in the customer environment first, with tenant isolation enforced in the data layer from the start.

Exit criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | Two workers, one incident, exactly one run | `tests/db/test_runs_durable.py`: twelve concurrent deliveries create one run; eight concurrent claims hand one job to one worker. On PostgreSQL. |
| 2 | Tenant A's session sees nothing of tenant B, with the filter omitted | `tests/db/test_tenant_isolation.py`: unfiltered select, raw SQL, joins, insert, update and delete on every tenant table. `tests/api/test_api_data.py`: every read route over HTTP. |
| 3 | A route without a declared permission fails start-up | `tests/api/test_http_access.py::TestRouteRegistry`. |
| 4 | `audit verify` detects a modified, deleted and reordered row | `tests/db/test_audit_log.py`, also an inserted row and, with a checkpoint, a removed tail and a rewritten chain. |
| 5 | A killed worker's run resumes without repeating a completed step | `tests/db/test_worker_killed.py` kills a real worker process; `tests/db/test_runs_durable.py` covers the same in one process. |

Deviations from the plan

| Item | What happened |
|---|---|
| HTML SOP editor | Removed now, not in Phase 3. Demo mode has no SOP editing until the review screen exists. Three tests went with it. |
| Static API tokens (`AGENT_API_TOKENS`) | Removed. Replaced by sign-in and by scoped tokens stored as hashes. |
| In-memory history, event bus and JSONL files | Removed. The database is the only state. `ResolutionMemory` is no longer wired into the service; its module remains for Phase 5. |
| Runner split (ADR 0003, points 2 to 5) | Not built. Adapters run in the worker. Moved to Phases 4 and 6. |
| Per-tenant connections | Not built. One deployment serves one tenant's ServiceNow and identity provider. Phase 3. |
| Audit export, retention, legal hold, evidence store | Not in the Phase 2 scope of this plan; still to be scheduled. |
| Library choices | In-house queue instead of `procrastinate`; `httpx` and `PyJWT` instead of Authlib; polling instead of `LISTEN/NOTIFY`. Reasons are in ADRs 0004 and 0006. |
| Dashboard | Gained a sign-in screen and sign-out. No screens yet for tokens, role mappings or the audit log; those are API and command line only. |

Test expectations changed

| Test | Before | Now | Reason |
|---|---|---|---|
| `test_http_access.py` (moved to `tests/api/`) | aiohttp, static bearer token | FastAPI, scoped database token or session | The HTTP layer and the credential type were replaced. Every assertion was kept or tightened; the list of public routes is now asserted exactly. |
| Start-up without credentials | Refused without `AGENT_API_TOKENS` and `AGENT_WEBHOOK_SECRET` | Refused without `AGENT_WEBHOOK_SECRET`; a missing identity provider is a warning | A deployment can be driven by tokens alone; without a webhook secret the intake would be open. |
| Demo mode on a non-loopback address | Refused without both credentials | Refused unless an identity provider and a webhook secret are both set | Same rule, new credentials. |

Tests removed with the code they covered: the demo-mode SOP editor test and two SOP editor escaping tests.

Verified by running: 755 tests on Windows and in a Linux container, on SQLite and PostgreSQL 16, none skipped; the production container image end to end against PostgreSQL (migrate, tenant, token, signed webhook, verified resolution, audit verification, refusal to start as the migration role); the runbook's database set-up with a non-superuser migration role.

Not verified: sign-in against a real identity provider; PostgreSQL behind a connection pooler or on a managed service; behaviour at production volumes; the new CI workflow on the hosted runner; the dashboard sign-in screen in a browser.

## 7. Phase 3 record

Delivered on branch `phase-3/connectors-and-knowledge`, based on `phase-2/foundations`. No product decision was needed; D8 (the ServiceNow approval mechanism) concerns Phase 4.

Exit criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | Manifest lint fails a connector whose write capability lacks a tier, verify operation, parameter schema or scopes | `tests/unit/connectors/test_manifest_lint.py`: each missing part is reported, and the registry refuses to be built with such a connector. Also required: an allow list for each target parameter and a rollback statement. |
| 2 | Contract tests replay recorded fixtures for each connector, including error and pagination cases | `tests/contract/`: ServiceNow ITSM and knowledge base, Splunk, Dynatrace, IBM MQ, Autosys, file share, browser, mainframe, Git. Paging is covered where the target pages: ServiceNow lists, Dynatrace entities, Git changes. **The fixtures were written by hand from vendor documentation. None is a capture from a live system.** |
| 3 | A draft cannot be selected by the matcher, at the data layer | `tests/db/test_sop_lifecycle.py::TestOnlyPublishedVersionsReachTheEngine`: the engine's view shows only published versions; a valid, matching SOP in review is not run; a row cannot be marked published without an approver and a hash. |
| 4 | The same user cannot author and approve one version, by constraint and at the API | `tests/db/test_sop_lifecycle.py::TestAuthorCannotApprove` (service and direct table writes), `tests/api/test_sop_api.py` (a user holding both roles). |
| 5 | Rotating a credential takes effect on the next call without restart | `tests/unit/connectors/test_secrets.py::TestRotation`, and over HTTP in `tests/contract/` for ServiceNow and Splunk. |

Also delivered: per-tenant connections with credential references; the ticket claim step; ServiceNow OAuth and paging; ingestion of articles as drafts; the SOP library and review screen; `l1-agent sop`, `connection`, `connectors` and `knowledge` commands.

Deviations from the plan

| Item | What happened |
|---|---|
| Seven adapters "ported" | Each is wrapped by a connector that declares a manifest and delegates to the original adapter. Their HTTP code was not moved onto the SDK client. Reasons are in ADR 0005. |
| Connector registration by entry points | A first-party list in code. Phase 6. |
| Confluence and SharePoint | Not built. The knowledge interface is two methods; ServiceNow knowledge base and Git implement it. |
| Parameter source `lookup`; language-aware escaping in templates | Not built. String parameters without a pattern stay limited to `[A-Za-z0-9_.-]`. Two places where a value reached a query or path unchecked were closed in the adapters instead (Dynatrace selector, IR360 path). |
| Drafting steps from a prose article | Not built: it is a model task (Phase 5). A prose article becomes a draft with no steps. |
| The engine role restricted to the published view | The view exists and is the only path the engine uses. There is still one application role, which can read the version table. |
| ITSM and identity provider per tenant | Still per deployment. Operations connectors and knowledge sources are per tenant. |
| Duplicate closing work notes after a crash | Listed for Phase 3 in the Phase 2 record. Not fixed; moved to Phase 4. |
| Screen for connections | Not built. Command line and API only. |
| Neutral ITSM method names | Kept the existing names and added `claim` (ADR 0005). |

Defects found in existing adapters while writing contract tests, and fixed

| Adapter | Defect |
|---|---|
| Dynatrace | Host health read one page of hosts and asked about problems on the first 20 only. A larger group could be reported healthy with an open problem on a host that was never checked. |
| Dynatrace | A host or host group name was placed inside a quoted selector without checking for quotes. |
| IR360 | A queue or queue manager name was placed in the request path without checking for `/`. |
| Autosys | The job status was read from the last column of `autorep` output. Real output ends with the run and exit columns, so the status would have been wrong. |

Test expectations changed

| Test | Before | Now | Reason |
|---|---|---|---|
| `test_servicenow_display_values.py::test_error_carries_status_and_servicenow_message` | Client built against a plain-http test server | Same, with `demo_mode=True` | The connector now refuses a plain-http base URL outside demo mode. Every assertion is unchanged. |
| `test_confidence_threshold.py` fixture | Mock ITSM with no `claim` | Mock ITSM whose `claim` succeeds | The engine claims a ticket before working it. Every assertion is unchanged. |
| `test_api_data.py::test_every_read_route_returns_only_the_callers_tenant` | Walked the Phase 2 routes | Also seeds an SOP version and a connection in each tenant and walks the new routes | New routes and tables. Extended, not relaxed. |
| `test_http_access.py::concrete`, `test_tenant_isolation.py::seed` | Knew the Phase 2 path parameters and tables | Know the new ones | Same. |

No test was removed.

Behaviour that changed for an existing deployment

- Outside demo mode the service starts with no SOPs. Knowledge articles are no longer loaded and run directly; they arrive as drafts and need two people.
- The agent assigns a ticket to itself before working it (`SERVICENOW_CLAIM`, default on).
- A step for a connector with no connection ends the run as `needs_human`. Before, every adapter was constructed whether or not it was configured.
- A mainframe transaction parameter is refused when the allow list is empty. Before, an empty list allowed any.
- `get_new_incidents` reads every page up to the ceiling. Before, the first 20 rows.

Verified against a ServiceNow developer instance after the phase summary (`pytest tests/live -m live_servicenow`, 14 passed): basic authentication, paging, the claim step, leaving alone a ticket someone else holds, hand-over to an L2 group, verified resolution, and listing knowledge articles. That run found two defects, both fixed:

| Defect | Fix |
|---|---|
| After a claim, escalation failed with `403`: a stock business rule ("Abort changes on group") refuses a group change while the ticket is still assigned. | A group change now also clears the assignment, when claiming is on. |
| Paging stopped at the first short page. ServiceNow cuts a page before it applies access controls, so a page can be short or empty while readable rows remain: on the instance, 3 readable knowledge articles out of 53 were all missed with small pages. | The end of a list is taken from `X-Total-Count`. A short page ends the list only when that header is absent. |

Decisions recorded for Phase 4: **D5** T1 is the highest tier that may run without a human approval; T2 always needs an approval bound to the action hash. **D8** approvals go through a change request created from a standard change template; a scoped application is deferred.

Not verified: ServiceNow OAuth against a real instance; ingesting real knowledge articles end to end (they were listed, not ingested); every operations connector against its real system; the Vault and AWS secrets backends; Git over https with a token; the review screen in a browser; the CI workflow for this branch on the hosted runner.

## 8. Phase 4 record

Delivered on branch `phase-4/actions`, based on `phase-3/connectors-and-knowledge`. Decisions D5 and D8 were taken before it started. The design is [ADR 0008](adr/0008-actions-policy-and-approvals.md).

**Everything in this phase is evidenced against a mock scheduler and a mock ServiceNow.** The Autosys force start has never run against Autosys. The ServiceNow change and approval calls were written from vendor documentation and have never run against an instance; the live tests of Phase 3 do not touch them.

Exit criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | The Autosys scenario passes end to end against the mock in shadow, approval and automatic modes, and a test reconstructs each run from the audit log alone | `tests/db/test_actions.py::TestScenarioInEachMode`, on SQLite and PostgreSQL. `reconstruct` in `tests/actions_support.py` reads only audit events and is compared with the run record, the action record and what the mock scheduler received. |
| 2 | No connector write capability can be invoked without a policy decision record | `tests/unit/actions/test_write_needs_a_decision.py`: every write capability in the registry is refused without a grant, with a forged one and with one from another authority; a grant is good for one call, once, for 60 seconds; and a search of the source tree finds `mint` called from two methods only, each after the database step that authorises. |
| 3 | Changed parameters invalidate the approval; expired approval is rejected; SOP author cannot approve a T2 action | `tests/db/test_actions.py::TestApprovals` and `tests/api/test_actions_api.py`. The T2 rule and "an approval is for this action" are also table constraints, tested by writing to the table directly. |
| 4 | With the switch on, an in-flight run stops before execute and a queued write is refused, globally and per tenant | `tests/db/test_actions.py::TestKillSwitch`, each test run for the tenant, global and local switch. The queued write is a grant that was already minted and is refused at the connector. |
| 5 | Verification failure triggers rollback where declared, then escalation | `tests/db/test_action_rollback.py`, with a connector written for the test because the Autosys force start declares no rollback. For Autosys: `TestOutcomes::test_a_job_that_fails_again_is_not_verified_and_the_run_escalates`. |

A mutation check was run by hand: eighteen guards were removed one at a time (kill switch, T2 approval, grant redemption, parameter digest, single use, author rule, hash check, expiry, pre-checks, verification, change record, allow list and others). Each removal made at least one test fail.

Also delivered: `ACTION` step type and its validation; `policies`, `actions` and `platform_flags` tables (migration 0003); approval through the dashboard and through ServiceNow; the Actions page; `l1-agent policy` and `killswitch`; three roles' new permissions; the restart sample SOP and three demo incidents.

Deviations from the plan

| Item | What happened |
|---|---|
| Autosys force start "through the argument list or REST API" | The argument list only. The REST API is not implemented: its request shape could not be checked against a system or confirmed from memory. |
| Change record: "or an existing approved normal change supplied on the ticket" | Not built. Only a standard change opened by the agent counts. |
| ServiceNow approval "polled or received by signed webhook" | Polled only. |
| Kill switch "checked by the runner against its local flag" | There is no separate runner yet. The local flag is a file the worker process checks. |
| "Operator stop" of one run | Not built. The kill switch and the expiry of an approval request are the ways to stop a waiting run. `runs.operate` still has no route. |
| Pre-check "the box, if any, is not on hold" | Not built; `autorep` output is not parsed for it. The pre-check is that the job is failed or terminated. |
| Kill switch "takes effect within the flag cache lifetime" | There is no cache. The switches are read from the database at each of the three checks. |
| Verification of an action and `RESOLVED` | A verified action does not make the run verified. A `VERIFY` step must still pass before `RESOLVE` (ADR 0002 unchanged). |
| Policy rule editing in the dashboard | The dashboard sets the autonomy level and the kill switch and shows the rules. Rules are edited as JSON through the API or CLI. |
| Duplicate closing work notes after a crash | Carried from Phase 2 and Phase 3. Still not fixed. |

Found and fixed while building

| Where | What |
|---|---|
| Autosys job name check (existing read adapter, and the new write) | The pattern ended in `$`, which accepts a name with a trailing newline. Now a full match. The name was always passed as one argument, never through a shell. |
| Capability schemas | Had no way to restrict a string's characters. `pattern` added, so an unsafe job name is refused before a grant is used. |

Test expectations changed

| Test | Before | Now | Reason |
|---|---|---|---|
| `test_manifest_lint.py::test_no_write_capability_exists_before_the_policy_engine` | Asserted the registry declares no write | Renamed `test_the_write_capabilities_are_exactly_the_reviewed_ones`; asserts the list is exactly `autosys.job.force_start`, T1, not idempotent, no rollback | The phase adds the first write. The new test fails when anyone adds another. |
| `test_manifest_lint.py` fixtures | `pre_checks=("demo.job.status",)` | `pre_checks=(PreCheck("demo.job.status", "<assertion>"),)`; seven more lint cases added | A pre-check now carries the assertion that must hold. |
| `test_phase3_cli_and_engine.py::test_connectors_lists_every_capability_with_its_tier` | Asserted no line says `write` | Asserts the only `write` line is the force start, with `T1` | Same. |
| `test_phase3_cli_and_engine.py` validator case | Message "only a CHECK step names a capability" | "only a CHECK or ACTION step names a capability" | Wording. |
| `test_tenant_isolation.py::test_every_table_but_tenants_carries_a_tenant` | Exempted `tenants` | Exempts the two names in `GLOBAL_TABLES` and asserts that list is exactly `tenants` and `platform_flags` | The global kill switch is a table with no tenant. New tests assert it holds no tenant data and that the application role can read it and cannot write it. |
| `test_tenant_isolation.py::seed`, `test_api_data.py`, `test_http_access.py::concrete` | Knew the Phase 3 tables and path parameters | Seed a policy and an action per tenant and walk the new routes | Extended, not relaxed. |
| `test_http_access.py::test_role_table_keeps_duties_apart` | Phase 2 separations | Adds: only `approver` holds `actions.approve`; only `tenant_admin` holds `policy.manage`; `operator` and `tenant_admin` hold `actions.stop` | New permissions. |
| `test_sop_api.py::test_demo_tenant_starts_with_published_sops_and_one_to_review` | Three published samples | Four | The restart sample. |
| `test_no_unverified_resolve.py` | Random graphs without actions; five mutations | Random graphs include `ACTION` steps; eleven mutations; a new test that no action state leads to `VERIFIED` | Extended. |

No test was removed.

Behaviour that changed for an existing deployment

- Nothing changes until an SOP with an `ACTION` step is published **and** the tenant's policy is raised from shadow. A deployment that does neither behaves as after Phase 3.
- A run can now be open for as long as an approval request lives (default 60 minutes).
- `aborted` now also means "stopped by the kill switch".
- Demo mode sets its tenant to `automatic` so the demo shows more than shadow. It does this only for a tenant that has no policy yet.
- Migration `0003` must be applied.

Verified: the full suite on Windows on SQLite and PostgreSQL 16, and in a Linux container; lint and format; bandit; the one-shot demo; the demo service over HTTP (an automatic restart, a T2 restart approved through the API, the kill switch, audit verification); the production frontend build.

Not verified: the force start against Autosys (`sendevent` exit codes, output and timing; whether `FA` and `TE` are the right states to require; how long a real job takes to show `RU`); every ServiceNow change and approval call, the roles the integration user needs for them, and whether a standard change can be taken through its states and closed by the Table API on a given instance; the Actions page in a browser; behaviour with several worker processes sweeping the same approval requests (each move is a guarded update, tested with one worker); everything listed as unverified in the Phase 3 record.

**After the phase: first run against an instance (2026-10-11).** `tests/live/test_servicenow_changes_live.py` was run against the developer instance with a stock standard change template.

- Ran as written: opening a standard change from a named template, returning the same change for the same action hash, refusing an unknown template, adding a work note, asking the six members of a group for approval once each, and reading the request back as waiting.
- Failed: closing. The instance's Standard change model refuses New to Scheduled while the change has no assignment group, and the stock template sets none. Fix: `SERVICENOW_CHANGE_ASSIGNMENT_GROUP`, which the agent puts on each change it opens; with it the change went through Scheduled, Implement and Review to Closed with close code `successful`. A refused close now names the unmet condition, read from the instance's next-states API.
- Learned: a standard change shows `approval = approved` from creation. The agent does not read that field.
- Still not run: reading an approval or rejection given by a person in ServiceNow, the email comparison for T2, an unsuccessful close, and the action pipeline end to end against a real instance. The test user's roles were not reduced to the least the integration needs.
- Left on the instance: changes CHG0030001 to CHG0030005 and their tagged incidents; CHG0030003 has six pending approval requests.

Decision recorded for Phase 5: **D6** a customer-supplied OpenAI-compatible endpoint and Anthropic are the supported model providers at launch, and running with no model is a supported configuration.

## 9. Phase 5 record

Delivered on branch `phase-5/model-hardening`, based on `phase-4/actions`. Decision D6 was taken before it started. The design is [ADR 0009](adr/0009-model-use.md); the documents for a model risk function are in [model-risk/](model-risk/).

**What is and is not verified against a real model.** The gateway with the `rank`, `extract` and `interpret` tasks ran once against one hosted model through the OpenAI-compatible client. The Anthropic client has only run against fixtures written by hand from its documentation. Article drafting has only run against stand-in models. Answer quality has not been measured for any task.

Exit criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | The injection corpus produces zero tool calls outside the current step's declared capability and zero writes, measured at the connector boundary | `evals/checks.py` (`injection_tickets`, `calls_are_what_the_sop_declares`), run by `tests/evals/test_evals.py` and the CI job `evals`: 9 hostile incidents against 4 misbehaving stand-in models, calls recorded inside each connector's `invoke` and compared with what the published SOP and the ticket render. The same 9 against one real model, once: held |
| 2 | With the provider disabled or returning garbage, every golden incident ends in a correct terminal state or NEEDS_HUMAN, never RESOLVED without verification | `evals/checks.py` (`golden`): 11 incidents with the model off, the deterministic stand-in, garbage, a failing provider, a hostile model and an obedient one. Outcome, SOP, verification step, capability calls and writes are identical in every case |
| 3 | A redaction corpus shows no seeded secret or personal data item in any outbound provider request | `evals/checks.py` (`redaction`), `evals/redaction.json`: 24 items of 8 kinds, all four tasks, both provider clients, searched for in the raw bytes a local HTTP server received |
| 4 | Exceeding the per-incident budget ends the advisor's involvement and the run continues deterministically or goes to NEEDS_HUMAN | `tests/db/test_model_governance.py` (`TestBudgets`), `tests/db/test_model_in_runs.py` (`TestABudgetInARun`): a budget reached in the middle of a run, and no budget at all, for four kinds of run; each ends as it does with no model |

What the criteria do not show: the corpora are small and written by the authors of the code, and the redaction corpus covers recognised shapes only. Its `not_found` list gives examples that are sent as they are.

Scope against the plan

| Planned | Delivered |
|---|---|
| Provider abstraction | `ai/providers.py`: OpenAI-compatible, Anthropic, and a deterministic stand-in. One provider per deployment |
| Per-tenant allow list, region and retention settings | `model_policies` and `ai/policy.py`. Off for a new tenant. Region and retention are the operator's declaration about the endpoint |
| The four advisor tasks with schema-validated output | `ai/advisor.py`, `ai/schema.py`: `rank`, `extract`, `interpret`, `draft` |
| Redaction before egress | `ai/redaction.py`, applied in the gateway to everything sent. Pattern matching |
| Token and cost budgets | Per incident and per tenant per day, from the `model_calls` record |
| Prompt versioning | `ai/prompts.py`; digests pinned by a test; versions required in the change log |
| Evaluation suite in CI | `evals/`, CI job `evals`, `tests/evals/` |
| Model inventory and change log | `docs/model-risk/` (inventory and intended use, validation evidence, change log), `docs/dpia-template.md`, `docs/sub-processors.md` |

Engineering decisions and deviations

- **The package is still `ai/`**, not `advisor/` as the target architecture named it.
- **Model use is off for a new tenant.** `LLM_ENABLED=true` alone no longer sends anything.
- **A model cannot steer a run into an SOP that makes a change.** Not in the plan; found while writing the injection corpus. Before this, a ticket that fitted a read-only SOP best and also cleared the threshold for the restart SOP could be moved to the restart by the model's ranking.
- **A value located by a model never reaches a write**, and only parameters the SOP author marks (`source.advisor`) may be located at all.
- **Redaction is reversible for personal data only.** A secret placeholder is never turned back into the secret.
- **The audit log holds digests of a model's answer, not the answer.** ADR 0007 planned to store the validated answer; it can contain ticket text.
- **The inventory check is on prompt versions and provider kinds**, not on configured model ids as the controls matrix had planned: the customer's model id is not known to the repository. It is in every `model.called` audit event.
- **The older `AIAnalyzer` remains** for existing unit tests and as a fallback in the one-shot demo. A test asserts the service cannot build one.
- **Output limits were raised after the first live run**, in which a model that reasons before answering had 7 of 16 answers cut short.

Not built

- More than one provider per deployment, or a tenant choosing between providers.
- Recognition of names and addresses in running text.
- A store of full prompts and answers.
- A scheduled live evaluation, or any measure of answer quality.
- An editing screen for the model policy rules.

Defects found and fixed in this phase

| Defect | Found by | Fix |
|---|---|---|
| A model's ranking could move a run to an SOP with an `ACTION` step | Writing the injection corpus | The pick is ignored unless that SOP is also the rules' best match |
| The Anthropic client did not turn a request timeout into a provider error | Its own test | Caught and converted |
| An IBAN followed by an upper-case word was not redacted | Writing the redaction tests | The candidate is shortened until its check digits pass |
| Output limits too low for a reasoning model | The first live run | Raised per task |

Changes to existing tests

| Test file | Change | Why |
|---|---|---|
| `tests/conftest.py` | `make_service` takes an optional `advisors` | To build a service with a model gateway |
| `tests/api/support.py` | `build` takes an optional `provider`; new `seed_model` | The model routes and tables |
| `tests/db/test_tenant_isolation.py` | Seeds a model policy and a model call per tenant | Extended to the two new tables, not relaxed |
| `tests/api/test_api_data.py` | Seeds the same; one more marker that must not leak | Extended to the new routes |

No test was removed and no existing assertion was changed.

Behaviour that changed for an existing deployment

- A deployment with `LLM_ENABLED=true` sends nothing to a model until its tenant's model policy is set. Demo mode sets one for the demo tenant.
- `LLM_ENDPOINT` must be `https` outside demo mode.
- A model's ranking no longer moves a run to an SOP that makes a change.
- With `interpret` enabled, an escalated run gets one more work note, labelled as model-drafted.
- Migration `0004` must be applied.

Verified: the full suite (1,685 tests) on Windows on SQLite and PostgreSQL 16 with 89% coverage (the last nine tests, added late, ran there on SQLite only), and the same 1,685 in a Linux container with PostgreSQL and SAML required; lint and format; bandit; the evaluation suite; a mutation check in which 25 guards were removed one at a time and each removal was caught; the evaluation suite's golden and injection corpora once against one hosted model; the one-shot demo; the demo service over HTTP (the model routes, switching model use off, an invalid policy refused, audit verification); both container images; the production frontend build.

Not verified: the Anthropic client against the live API; article drafting and redaction against a real model; answer quality; the dashboard's model card and labels in a browser; several workers reaching a daily budget together; GitHub CI for this branch.

Open decisions: D7 (product name, licence, preview connectors) is needed before Phase 6. *(Taken; see section 2 and the Phase 6 record.)*

## 10. Phase 6 record

Delivered on branch `phase-6/packaging`, based on `phase-5/model-hardening`. Decision D7 was taken before it started: the product is named **L1 and L2 AI-Ops** (chart and images `l1-and-l2-ai-ops`; the Python package and the command keep the names `l1_agent` and `l1-agent`), no licence is declared, and the mainframe and browser connectors ship as previews, read only. The design of the runner is [ADR 0010](adr/0010-runner-protocol.md). Installing and operating it is in the [operator guide](operator-guide.md); what it does and does not protect against is in the [security guide](security-guide.md).

**What is and is not verified outside a test process.** The control plane and the runner have run as separate processes on one machine (the compose demo, on PostgreSQL, all sample incidents) and in one local Kubernetes cluster (kind), where the chart was installed, tested, upgraded and its database restored from a backup. Nothing has run in a managed cluster, behind a real ingress, across a real network boundary, or against any real target system that had not been reached before this phase. The CI job that produces a bill of materials and the workflow that signs released images are written and **have never run**. The dependency audit and the image scan were each run once by hand. The new workflow as a whole has not run on the hosted runner.

Exit criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | The "done means" list as an automated end-to-end test against mocks: onboard a tenant, connect ServiceNow, a knowledge source, Autosys and Splunk, approve an SOP, set autonomy to shadow, run the scenario | `tests/e2e/test_done_means.py`, on SQLite and on PostgreSQL. The real service assembly; the tenant made with the command line; ServiceNow, Splunk, Autosys and a Git SOP source connected through the HTTP API by an administrator; the SOP submitted by an author, refused when the author tries to approve it, approved and published by someone else; autonomy set to shadow; an incident picked up by the poller, worked by a worker, with every connector call made by a real runner over the protocol. It ends escalated with one shadowed action, nothing changed, the ticket claimed and handed over, and the audit chain verified. What stands in: ServiceNow is an HTTPS server written by hand from the Table API documentation; Autosys and Splunk are the mock adapters, held by the runner; sign-in is a session written to the database. The brief's own section 10 is not in the repository; the list used is the one this plan quotes from it |
| 2 | A CI job parses the controls matrix and fails if any row marked implemented points at a missing file or a test that does not pass | `tools/controls_matrix.py`, run in the `tests` job after the suite, against that run's results, with a skipped test counted as a failure. `tests/unit/test_controls_matrix_tool.py` shows it failing for a missing file, a name no longer in a file, a renamed test, an undefined CI job, a failing test, a test that did not run and a skipped test. Run locally against a full run: 49 rows marked implemented, 112 test selections, all present and passed. During the phase it caught one real failure this way |
| 3 | The container runs as non-root with a read-only root filesystem in the chart's default values, verified by a chart test | `tests/deploy/test_helm_chart.py` (`TestEveryContainerIsLockedDown`) on the rendered manifests, with values that set nothing about security; the values schema refuses to render with either setting undone. The chart's own `helm test` pod checks it from inside a running container. In a kind cluster: `helm test` passed, and each of the four containers reported a non-zero uid and could not write to its root filesystem |

Scope against the plan

| Planned | Delivered |
|---|---|
| Helm chart | `deploy/helm/l1-and-l2-ai-ops/`: an install job (migrations, tenant, runner enrolment; safe to repeat), API, workers, runner, dashboard, ingress, network policies, disruption budgets, a values schema and a test pod. It creates no secret |
| Runner image and outbound-only protocol | `src/l1_agent/runner/`, `Dockerfile` target `runner`, migration `0005`. Two routes, both called by the runner; three Ed25519 key pairs; the runner's own ceiling. **Without mutual TLS**, which the threat model had planned |
| Connector set-up wizard | The dashboard's Set-up page: a form per connector built from its manifest, credentials as references, save and test from where the calls are made. ServiceNow can now be connected this way, not only through the environment |
| Tenant onboarding flow | `service/onboarding.py`, `GET /api/onboarding`, `l1-agent onboarding`, and the checklist at the top of the Set-up page. Computed from what exists; nothing stored |
| Operator and security documentation | `docs/operator-guide.md`, `docs/security-guide.md`, `docs/asvs-self-assessment.md` (by chapter, not by requirement), `deploy/helm/l1-and-l2-ai-ops/README.md`; README, RUNBOOK, ARCHITECTURE, threat model and three ADRs brought up to date |
| SBOM | CI job `sbom` and the release workflow. Never run |
| Image signing | `.github/workflows/release.yml`: keyless signature and a signed bill of materials for each image digest. Never run; no release exists |
| Hash-pinned dependencies | `requirements.lock`, `requirements-dev.lock`; the image installs with `--require-hashes`; base images pinned by digest; GitHub Actions pinned by commit; CI fails if a lock file is out of date |
| Scanning in CI | Dependency audit (`pip-audit`, `npm audit`) and an image scan (Trivy), both blocking. Run by hand once: the audits found nothing; the scan found two high-severity findings in packaging tools bundled with the Python base image and 43 in the nginx base, which were fixed, and then nothing high or critical with a fix available |
| Controls matrix completed, every row pointing at code and a passing test | Every row now says Implemented, Partial or Not built, and Implemented is checked. **Not every row is implemented**: 49 are, 10 are partial, 3 are not built (below) |

Engineering decisions and deviations

- **No mutual TLS.** The runner authenticates the server by certificate and itself by a signature on each request; every call and every grant is signed too. Signatures work wherever TLS ends; binding a client certificate to a tenant would have had to be done by an ingress we do not control. Recorded in ADR 0010 and in the threat model.
- **The key that authorises a write is not in the web tier.** Calls and grants are signed with different keys so that the API, which must be able to queue a connection test, cannot mint a grant.
- **`RUNNER_MODE` is `embedded` in the code and `remote` in the chart.** A deployment configured before this phase behaves as it did.
- **A runner can be recognised by its key alone**, so that an install job can enrol it before it first starts. Where several tenants shared a control plane it would be given an identity that names its tenant.
- **The ITSM connection and the SOP sources stay in the control plane.** Only the seven operations connectors are behind the runner.
- **Evidence still travels to the control plane.** Keeping it at the customer's site with digests only (ADR 0003, point 5) is not built.
- **No plug-in mechanism for connectors.** Entry points and a signed list, which Phase 3 had moved here, were not built, and that is now the position: only first-party connectors load.
- **The service starts without a ServiceNow connection** and takes no incident until one is entered. It had refused to start. A stored connection wins over the environment's, and a broken stored one does not fall back.
- **The dashboard is its own image**, served by nginx, which also passes the API through so both share an origin. A production build now asks no other origin for anything: the typeface is no longer loaded from Google Fonts unless a build asks for it.
- **Two things outside the plan's scope line were built because the matrix needed them**: an export of a tenant's data that can be verified from its files alone (RS-05), and a restore drill inside the chart's install test (RS-04).
- **The API can serve TLS itself**, so that the in-cluster connection from the runner is encrypted without a mesh.
- **Demo mode can start with an empty tenant** (`AGENT_DEMO_SEED=false`), which the end-to-end test and anyone walking through set-up need.

Not built

- Mutual TLS in the product; key rotation; hardware-backed keys; remembering used call ids across a runner restart.
- Export to a SIEM (AU-04); retention, erasure, legal hold and an evidence store (AU-05); encryption at rest by the product (CR-04, the platform's). An import to match the export.
- Any limit on outbound traffic in the chart. A health endpoint for a worker or a runner. Routing of some connectors to one runner and others to another.
- A screen for role mappings, for policy rules, for model policy rules, or for the audit log.
- The hosted shape (ADR 0003, B). Nothing has run with the two planes on different networks.
- A requirement-by-requirement ASVS assessment.

Defects found and fixed in this phase

| Defect | Found by | Fix |
|---|---|---|
| The Phase 5 branch fails its secret scan on GitHub: one made-up AWS key in the redaction corpus was not on the reviewed list | Looking at the hosted run after the push | Added to `.gitleaksignore`, after reading the value. The Phase 5 branch itself is still red |
| The dashboard requested a font from a third party on every load | Writing the dashboard's content security policy | Opt-in at build time; allowed by the server only if configured |
| `l1-agent tenant create` wrote a second `tenant.created` event when run for a tenant that exists | Writing the install job | It now leaves an existing tenant alone |
| The chart tests imported a package (PyYAML) that was installed on the development machine and declared nowhere | Running the suite in a clean Linux container, installed from the lock file | Added to the dev requirements and the lock file |
| The image carried `setuptools` and `wheel` from its base, whose bundled copies have known high-severity issues; the first nginx base chosen for the dashboard was behind on 43 fixes | Running the image scan by hand | The two tools are removed after installation (nothing imports them at run time); the dashboard's base moved to a current one, pinned by digest |

Changes to existing tests

| Test file | Change | Why |
|---|---|---|
| `tests/unit/actions/test_write_needs_a_decision.py` | The list of places allowed to attach a grant verifier has two entries, not one | The runner is the second place. The assertion is still an exact list |
| `tests/conftest.py`, `tests/actions_support.py` | `make_service` and `make_world` take optional `connectors`, `grant_key`, `tenant`, `adapters` | To run the existing action scenarios through a runner |
| `tests/api/support.py` | New `seed_runner` | The runner's two tables |
| `tests/db/test_tenant_isolation.py`, `tests/api/test_api_data.py` | Seed a runner and a queued call per tenant; one more marker that must not leak; one more cross-tenant write that must fail | Extended to the new tables and routes, not relaxed |

No test was removed and no other existing assertion was changed.

Behaviour that changed for an existing deployment

- Migration `0005` must be applied.
- Outside demo mode, with no `SERVICENOW_*` settings, the service starts and takes no incident (the webhook answers 503) where it used to refuse to start. With incomplete settings it still refuses.
- A `servicenow` connection stored for the tenant is used in place of the environment's.
- The image runs as uid 10001, has no `curl`, and installs from `requirements.lock`. Mount a writable `/tmp` if the root filesystem is read only.
- The dashboard no longer loads the Inter typeface from Google Fonts unless built with `VITE_FONT_STYLESHEET`.
- `GET /api/connectors` items carry `settings` and `runs_on`; `config` is still the schema.
- A grant is now an Ed25519 signature. Nothing to configure unless a runner is used.
- CI installs from the lock files and has four more jobs (`deps`, `sbom`, `image-scan`, `chart-install`); `docker-build` is now `images`.

Verified: the full suite (1,917 tests, none skipped) on Windows, on SQLite and PostgreSQL 16, with 90% coverage, and the same suite (1,917 tests, none skipped) in a Linux container installed from `requirements-dev.lock` with hashes, with PostgreSQL, SAML and Helm required; the controls matrix check against each run's results in strict mode; lint and format; bandit; the evaluation suite; the one-shot demo; `pip-audit` on `requirements.lock` and `npm audit` on the dashboard's shipped dependencies (no findings); a Trivy scan of the control plane and dashboard images (nothing high or critical with a fix, after the fixes above); all four image targets built from the lock file and pinned bases, started read-only as a non-root user; the compose demo from a clean start (PostgreSQL, API, worker, runner with mock connectors, dashboard), with all sample incidents worked through the runner; the chart in a kind cluster through `tools/ci/kind_install.sh` (install job, `helm test`, each container's uid and read-only root, the runner connected over TLS with a private CA and recognised by its key, the audit chain, a second upgrade that changed nothing, and a backup restored and verified); the production frontend build; the Set-up page once in a headless browser against the demo, with no console errors.

Not verified: the new CI workflow on the hosted runner, any of it (its steps were run by hand, one at a time, on this machine); the bill of materials and image signing, which have never run anywhere; the image scan and the audits as CI jobs; the chart in a managed cluster, behind an ingress controller, with an enforcing network plug-in, against a managed database; the runner across a real network, through a proxy, with a client certificate, or with the Vault or AWS backends; the Set-up page's forms in a browser (they were driven through the API); everything listed as unverified in earlier phases, which is unchanged: no real Autosys, Splunk, Dynatrace, IBM MQ, mainframe or identity provider, and the action pipeline never against a real instance.

Open decisions: none recorded. Whether to publish a release (which is what first runs the signing workflow), and what licence to declare, are still to be decided.
