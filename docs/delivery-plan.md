# Delivery plan

Status: Phase 0 output, awaiting review. No product code has been changed.

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
| D5 | Highest tier allowed to run without a human approval. | T1 only. T2 always needs an approval bound to the action hash, in every autonomy level. This is a liability position as much as an engineering one. |
| D6 | Model providers at launch, and whether "no model" is a supported configuration. | Support a customer-supplied OpenAI-compatible endpoint and Anthropic. Make the engine fully functional with the model disabled, because some buyers will start that way. |
| D7 | Product name, licence, and whether the mainframe and browser connectors ship in the first release. | Ship both connectors as T0 read only and labelled preview. They carry the most risk for the least reference-case value. |
| D8 | ServiceNow approval mechanism: approval on a change request created from a standard change template, or a custom approval record. Whether we ship a ServiceNow scoped application. | Standard change template plus approval on the change request. A scoped application is the durable answer but adds store certification work; defer it to after Phase 6. |

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
| Legacy `ARCHITECTURE.md` and `RUNBOOK.md` | Customer name removed and a currency notice added. They were not rewritten and still describe removed modes in places. `README.md` was rewritten. |
| Requirements | Split into `requirements.txt` (runtime), `requirements-dev.txt` and `requirements-connectors.txt` instead of one file. |
| Demo mode ServiceNow | Demo mode now uses an in-memory ServiceNow stand-in; before, it called the real client with an empty URL and logged an error per note. |
| Polling in demo mode | Disabled, for the same reason. |

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

Tests removed with the code they covered: `tests/unit/ai/test_ai_executor.py` (7 tests), nine investigation-tool cases in `test_tool_definitions.py`, one in `test_mock_llm.py` (replaced by a test that the mock makes no operational tool call).

Not done in Phase 1, by design: persistence, tenancy, SSO and roles, audit log, connector SDK, SOP review workflow, write actions, redaction evaluation corpus. See phases 2 to 5.
