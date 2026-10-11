# Controls matrix

> **Engineering input for review by a compliance professional.** This document is written by engineering. It is not legal advice, not an audit opinion, and not a claim of certification, attestation or conformity with any framework named here. The product holds no certification. Framework references are given at criterion, control or article level from engineering's reading and must be checked against the current published texts by a qualified reviewer before any customer relies on them.

Status: after Phase 5. Rows marked Implemented or Partial name code and tests that exist and pass now; each says what it does not cover. **Every other row is still a planned control** with the location it will have. Evidence is from automated tests against mocks, hand-written fixtures, connector conversations written by hand from vendor documentation, identity providers faked in the tests and a PostgreSQL 16 container. The only real external systems exercised so far are one ServiceNow developer instance (incidents, knowledge articles, and opening, noting and closing a standard change and asking for its approval; not reading a person's approval) and one hosted model provider (rank advice in Phase 1; the gateway with the rank, extract and interpret tasks once in Phase 5). **Every control about actions is evidenced against a mock scheduler and a mock ServiceNow**; the action pipeline has never run against a real instance. A row moves to "Implemented" only when the code exists and its test passes; that is an engineering statement, not an audit result.

Related: [target architecture](target-architecture.md), [threat model](threat-model.md), [delivery plan](delivery-plan.md).

## 1. How to read this

- **Code** and **Test** are paths relative to the repository root. In Phase 0 they are the intended locations.
- **Status**: Planned (phase), Partial (something exists today, described in section 3), Implemented (code and passing test).
- Framework abbreviations: SOC 2 = AICPA Trust Services Criteria; ISO = ISO/IEC 27001:2022 Annex A; ASVS = OWASP Application Security Verification Standard chapter; LLM = OWASP Top 10 for LLM Applications 2025; DORA = Regulation (EU) 2022/2554; AI Act = Regulation (EU) 2024/1689; RMF = NIST AI RMF 1.0 function; 42001 = ISO/IEC 42001:2023; SR 11-7 = US Federal Reserve and OCC model risk guidance; SOX = IT general control domain.

## 2. Control catalogue

### Access control and identity

| Id | Control | Maps to | Code | Test | Status |
|---|---|---|---|---|---|
| AC-01 | Users authenticate through the customer's identity provider (OIDC, SAML); no local passwords except one audited break-glass account | SOC 2 CC6.1; ISO A.5.16, A.5.17, A.8.5; ASVS Authentication; SOX logical access | `src/l1_agent/auth/oidc.py`, `auth/saml.py`, `auth/breakglass.py` | `tests/api/test_sign_in.py`, `tests/api/test_saml.py` | Implemented (2) against identity providers faked in the tests. Not run against a real identity provider. SAML needs the optional `requirements-saml.txt` |
| AC-02 | Every route, including SSE, metrics and health detail, declares a permission; start-up fails otherwise | SOC 2 CC6.1, CC6.3; ISO A.5.15, A.8.3; ASVS Access Control | `src/l1_agent/api/access.py` | `tests/api/test_http_access.py` (`TestRouteRegistry`, `TestEveryRouteNeedsCredentials`) | Implemented (2) |
| AC-03 | Seven roles with a fixed role-to-permission table; role assignment from identity provider groups | SOC 2 CC6.2, CC6.3; ISO A.5.18, A.8.2; SOX logical access | `src/l1_agent/auth/permissions.py`, `auth/store.py` | `tests/api/test_http_access.py` (`TestPermissions`), `tests/api/test_sign_in.py` (`TestRoleMappings`) | Implemented (2). SOP authoring and approval routes exist from Phase 3; action approval, policy and kill switch routes from Phase 4. `test_role_table_keeps_duties_apart` pins who holds each of them |
| AC-04 | Scoped, expiring, hashed API tokens for machine callers | SOC 2 CC6.1; ISO A.5.17; ASVS API | `src/l1_agent/auth/store.py` | `tests/api/test_sign_in.py` (`TestApiTokens`) | Implemented (2). A token cannot carry administrative or approval permissions |
| AC-05 | Tenant isolation by row level security with a non-owner application role | SOC 2 CC6.1, C1.1; ISO A.8.3, A.5.23; ASVS Access Control | `src/l1_agent/db/migrations/versions/0001_initial_schema.py`, `src/l1_agent/db/engine.py` | `tests/db/test_tenant_isolation.py`, `tests/api/test_api_data.py` (`TestTenantIsolationOverHttp`) | Implemented (2) on PostgreSQL 16. Not tested behind a connection pooler. Demo mode (SQLite) has one tenant and no row level security |
| AC-06 | Webhooks authenticated by HMAC with timestamp and replay protection | SOC 2 CC6.6; ISO A.8.21; ASVS API | `src/l1_agent/api/access.py`, `src/l1_agent/auth/store.py` | `tests/api/test_http_access.py` (`TestWebhookSignature`) | Implemented (2): replays are recorded in the database, so every replica sees them. Still one shared secret per deployment; per-connection secrets Planned (3) |
| AC-07 | Sessions are server-side with secure cookie attributes; CSRF tokens on state-changing requests | ASVS Session Management; ISO A.8.26 | `src/l1_agent/auth/store.py`, `src/l1_agent/api/access.py` | `tests/api/test_sign_in.py` (`TestSessions`) | Implemented (2) |
| AC-08 | Rate limits, body size limits, strict CORS, security headers | SOC 2 CC6.6; ISO A.8.20, A.8.26; ASVS Configuration | `src/l1_agent/api/middleware.py`, `src/l1_agent/api/app.py` | `tests/api/test_http_access.py` (`TestRateLimit`, `TestWebhookSignature`, `TestEveryRouteNeedsCredentials`) | Implemented (2). The rate limit is counted per process, not across replicas |

### Segregation of duties and change management

| Id | Control | Maps to | Code | Test | Status |
|---|---|---|---|---|---|
| SD-01 | An SOP version cannot be approved by its author | SOC 2 CC8.1; ISO A.5.3, A.8.32; SOX change management, segregation of duties | `src/l1_agent/sop/store.py` (`approve`), constraint `ck_sop_versions_two_people` in `src/l1_agent/db/models.py` | `tests/db/test_sop_lifecycle.py` (`TestAuthorCannotApprove`), `tests/api/test_sop_api.py` | Implemented (3): refused by the service and by a table constraint, on PostgreSQL and SQLite. Identity is the signed-in subject; two accounts held by one person are not detected |
| SD-02 | Published SOP versions are immutable, versioned and hashed; every run records id, version and hash | SOC 2 CC8.1, PI1.2; ISO A.8.32, A.8.9; SOX change management | `src/l1_agent/sop/store.py`, `src/l1_agent/db/migrations/versions/0002_connections_knowledge_and_sop_versions.py`, `src/l1_agent/service/runs.py` | `tests/db/test_sop_lifecycle.py` (`TestImmutability`, `TestLifecycle`, `TestResume`, `TestOnlyPublishedVersionsReachTheEngine`) | Implemented (3) on PostgreSQL: a trigger refuses changes to a submitted version for every role. SQLite (demo) has the constraints but no trigger. Runs record id, version and hash |
| SD-03 | A draft SOP cannot execute | SOC 2 CC8.1; ISO A.8.31, A.8.32 | `src/l1_agent/sop/store.py` (`published`), view `published_sops`, `src/l1_agent/service/runs.py` | `tests/db/test_sop_lifecycle.py` (`TestOnlyPublishedVersionsReachTheEngine`), `tests/db/test_ingest_and_connections.py` (`TestIngestion`) | Implemented (3). The engine reads through the view; the application role is not yet restricted to it |
| SD-04 | A T2 action cannot be approved by the author of the SOP version that requested it | ISO A.5.3; SOX segregation of duties | `src/l1_agent/actions/store.py` (`decide`), constraint `ck_actions_t2_two_people`, `src/l1_agent/actions/service.py` (`_collect`) | `tests/db/test_actions.py` (`TestApprovals`, `TestServiceNowApproval`), `tests/api/test_actions_api.py` | Implemented (4): refused by the service and by a table constraint. For an approval made in ServiceNow the two identities are compared by email, and the approval is not accepted when either is unknown. Two accounts held by one person are not detected |
| SD-05 | Every write is linked to a change record or a pre-approved standard change template; otherwise denied | SOC 2 CC8.1; ISO A.8.32; SOX change management; DORA Art. 9 | `src/l1_agent/actions/service.py` (`_ensure_change`), `src/l1_agent/connectors/itsm/servicenow.py` (`open_change`) | `tests/db/test_actions.py` (`test_no_change_record_no_write`, `test_an_itsm_without_change_management_cannot_back_a_write`), `tests/contract/test_servicenow_change_contract.py` | Partial (4): a standard change is opened from the template the SOP names, carries the action hash, and is closed with the result; with no change the write is denied. An existing approved normal change is not accepted. Opening and closing have run against one developer instance, where closing needs an assignment group on the change (`SERVICENOW_CHANGE_ASSIGNMENT_GROUP` or the template) |
| SD-06 | Configuration, connector, role and policy changes are audited with actor and before and after digests | SOC 2 CC8.1, CC6.2; ISO A.8.9, A.8.15 | `src/l1_agent/auth/store.py`, `src/l1_agent/connectors/hub.py`, `src/l1_agent/sop/store.py`, `src/l1_agent/audit/log.py` | `tests/api/test_sign_in.py` (`TestRoleMappings`, `TestApiTokens`), `tests/db/test_ingest_and_connections.py` (`TestConnections`), `tests/db/test_sop_lifecycle.py` | Partial (3): role-mapping, token, connection and SOP changes are audited with the actor and a digest of the new value. The previous value's digest is not recorded. Policy changes and every use of a kill switch are audited from Phase 4 (`tests/db/test_actions.py::TestPolicyStore`, `TestKillSwitch`). Environment settings Planned (6) |

### Safe execution

| Id | Control | Maps to | Code | Test | Status |
|---|---|---|---|---|---|
| EX-01 | No outcome defaults; RESOLVED requires a passed verification after the last action | SOC 2 PI1.2, PI1.4; AI Act Art. 15; RMF Measure | `src/l1_agent/engine/state_machine.py`, `src/l1_agent/engine/executor.py`, `src/l1_agent/models/evidence.py` | `tests/unit/engine/test_no_unverified_resolve.py` | Implemented (1), in memory. Database constraint Planned (2) |
| EX-02 | SOPs are statically validated before publication and before load | SOC 2 PI1.2; ISO A.8.26 | `src/l1_agent/sop/validator.py`, `src/l1_agent/sop/store.py` (`submit`, `publish`) | `tests/unit/engine/test_expressions_and_parameters.py`, `tests/unit/engine/test_no_unverified_resolve.py`, `tests/db/test_sop_lifecycle.py`, `tests/db/test_phase3_cli_and_engine.py` | Implemented (3): at submission, at publication, at load and before execution |
| EX-03 | Idempotent intake: one run per incident enforced by unique constraint | SOC 2 PI1.3; ISO A.8.26 | `src/l1_agent/db/models.py`, `src/l1_agent/service/runs.py` | `tests/db/test_runs_durable.py` (`TestIntake`, `TestQueue`) | Implemented (2) |
| EX-04 | Every write requires a policy decision; the write entry point cannot be called without a grant | SOC 2 CC6.1, CC8.1; ISO A.8.2; LLM06; AI Act Art. 14 | `src/l1_agent/actions/policy.py`, `actions/service.py`, `actions/grants.py`, `src/l1_agent/connectors/sdk/base.py` | `tests/unit/actions/test_write_needs_a_decision.py`, `tests/unit/actions/test_policy.py` | Implemented (4): every write capability in the registry is refused without a grant; a grant is good for one call, once, for 60 seconds; a source-tree search finds the mint called only after the database authorisation. The grant is checked in the process that minted it; a separate runner needs a key split (6) |
| EX-05 | Approvals bound to action and parameter hash, with expiry and approver identity | SOC 2 CC8.1; ISO A.5.3; AI Act Art. 14; SOX change management | `src/l1_agent/actions/store.py`, `actions/service.py`, constraints `ck_actions_approval_is_for_this_action`, `ck_actions_executed_was_authorised`, trigger `actions_guard` (migration 0003) | `tests/db/test_actions.py` (`TestApprovals`) | Implemented (4): changed parameters void the approval; an expired request cannot be approved; an approval not acted on in time is void; a decision is made once. The trigger that makes a decided row unchangeable is PostgreSQL only |
| EX-06 | Autonomy levels per tenant and SOP; new tenants start in shadow | AI Act Art. 14; RMF Manage; 42001 Annex A (use of AI systems) | `src/l1_agent/actions/policy.py`, `actions/store.py` | `tests/unit/actions/test_policy.py` (`TestAutonomy`, `TestDefaults`, `TestProperties`), `tests/db/test_actions.py` (`TestScenarioInEachMode`, `test_a_newly_published_sop_version_starts_in_shadow`) | Implemented (4): shadow, recommend, approval, automatic; the lowest of tenant, SOP version and deployment ceiling applies; a tenant with no policy and a new SOP version are in shadow. Demo mode sets its own tenant to automatic |
| EX-07 | Kill switch, global and per tenant, checked in policy, worker and runner | AI Act Art. 14; RMF Manage 2.4; DORA Art. 11 | `src/l1_agent/actions/store.py` (`set_kill_switch`, `set_global_kill_switch`, `authorise_execution`), `actions/service.py` (`redeem`) | `tests/db/test_actions.py` (`TestKillSwitch`), `tests/db/test_tenant_isolation.py` (`TestGlobalTables`) | Implemented (4): tenant, global and local switches, read at the decision, in the authorising transaction and when the connector redeems the grant. The runner check is the same process until the runner is split out (6) |
| EX-08 | Action pipeline: pre-checks, dry run, execute, verify, rollback | SOC 2 PI1.4, CC7.4; ISO A.8.32 | `src/l1_agent/actions/service.py`, `src/l1_agent/connectors/ops/autosys.py` | `tests/db/test_actions.py`, `tests/db/test_action_rollback.py`, `tests/contract/test_ops_contract.py` (`TestAutosys`) | Implemented (4) against a mock scheduler and a mock servicenow only. Rollback is exercised with a connector written for the test, because the one shipped write declares none |
| EX-09 | Connector parameters validated against closed schemas and allow lists; empty list denies | ISO A.8.26, A.8.28; ASVS Validation; LLM05 | `src/l1_agent/connectors/sdk/manifest.py`, `connectors/sdk/base.py`, `connectors/hub.py`, `src/l1_agent/engine/parameters.py` | `tests/unit/connectors/test_manifest_lint.py` (`TestEntryPoint`), `tests/contract/test_ops_contract.py`, `tests/unit/security/test_hardening.py` | Implemented (3): closed schemas from the manifests and per-tenant allow lists, checked before connector code runs. Path, host and transaction matching is done by the connector, tested against fixtures only |
| EX-10 | Connector manifest lint: write capabilities declare tier, scopes, verify and rollback | SOC 2 CC8.1; ISO A.8.25 | `src/l1_agent/connectors/sdk/manifest.py` (`lint`), `connectors/sdk/registry.py` | `tests/unit/connectors/test_manifest_lint.py` | Implemented (3). From Phase 4 one write capability is declared and passes it; pre-check and verify assertions are parsed by the lint |

### Model governance

| Id | Control | Maps to | Code | Test | Status |
|---|---|---|---|---|---|
| ML-01 | The model has no tool access; output is parsed against a closed schema into one of four advisor results | LLM01, LLM05, LLM06; AI Act Art. 14, 15; RMF Manage | `src/l1_agent/ai/advisor.py`, `ai/gateway.py`, `ai/schema.py`, `src/l1_agent/engine/incident_processor.py`, `engine/executor.py` (`_bind`), `engine/parameters.py` (`accept_advised`) | `tests/unit/ai/test_model_boundary.py`, `tests/unit/ai/test_schema_and_prompts.py`, `tests/unit/engine/test_advised_parameters.py`, `tests/db/test_model_in_runs.py`, `tests/evals/test_evals.py` (injection) | Implemented (5): four tasks, each with a closed answer schema; the gateway is the only caller of a provider (source-tree search); the injection corpus shows no call outside the step's declared capability and no write, counted inside the connectors, against stand-in models and once against one real model. A model can still choose between read-only SOPs that both fit, point at other text in the same ticket, and write a misleading labelled note |
| ML-02 | Redaction of secrets and personal data before any provider request | LLM02; GDPR Art. 5(1)(c), 25, 32; ISO A.8.11, A.8.12 | `src/l1_agent/ai/redaction.py`, `ai/gateway.py` (`compose`) | `tests/unit/ai/test_redaction.py`, `tests/evals/test_evals.py` (redaction), `evals/redaction.json` | Implemented (5) for recognised shapes: 24 seeded items of 8 kinds are absent from every request body, for four tasks and both provider clients. Pattern matching only: a name in running text (other than the ticket's caller), a postal address and a secret with no recognisable shape are sent. The corpus lists examples it does not catch |
| ML-03 | Per-tenant provider allow list, region and retention settings enforced in code | GDPR Art. 28, 44 to 49; DORA Art. 28; ISO A.5.19, A.5.23; LLM03 | `src/l1_agent/ai/policy.py` (`permits`), `ai/gateway.py` | `tests/unit/ai/test_model_policy.py`, `tests/db/test_model_governance.py` (`TestThePolicyIsReadForEveryRequest`), `tests/api/test_model_api.py` | Implemented (5): a tenant with no policy sends nothing; provider, task, region and retention are checked before every request, with no cache. Region and retention are what the operator declares about the endpoint; the product cannot verify them. One provider per deployment |
| ML-04 | Token and cost budgets per incident and tenant | LLM10 | `src/l1_agent/ai/gateway.py` (`_refusal`), `ai/policy.py` (`spent`, `stopped`) | `tests/db/test_model_governance.py` (`TestBudgets`), `tests/db/test_model_in_runs.py` (`TestABudgetInARun`) | Implemented (5): tokens and cost, per incident and per tenant per day, checked before a request is sent; past a budget the advisor is out of that run and the run ends as it would with no model. The daily budget is checked without a lock and can be passed by the requests in flight |
| ML-05 | Each model call audited: provider, model id, prompt version, input and output digests | AI Act Art. 12; RMF Measure 2.8; 42001 Annex A (event logs); SR 11-7 ongoing monitoring | `src/l1_agent/ai/policy.py` (`record_call`), migration `0004` (`model_calls`, append only) | `tests/db/test_model_governance.py` (`TestTheRecordOfACall`) | Implemented (5): provider, model id, prompt version and digest, SHA-256 of the request and of the validated answer, token counts, cost and outcome, in the same transaction as the call's row. Neither prompt nor answer text is stored |
| ML-06 | Evaluation suite with golden incidents and an injection corpus runs in CI | RMF Measure 2; 42001 Annex A (verification and validation); SR 11-7 validation; AI Act Art. 15; LLM01 | `evals/` (`golden.json`, `injection.json`, `redaction.json`, `checks.py`) | CI job `evals` (`python -m evals.run`), `tests/evals/test_evals.py` | Implemented (5) against stand-in models that obey injected text, give the worst valid answer, return garbage, or fail. `python -m evals.run --live` runs the golden and injection corpora against a real provider on demand; run once, against one model. The corpora are small and written by the authors of the code |
| ML-07 | Model inventory, intended-use statement, limitations, validation evidence and change log are maintained as documents | SR 11-7; RMF Govern 1.6, Map; 42001 clause 8; AI Act Art. 11, 13 | `docs/model-risk/` (`model-inventory.md`, `validation.md`, `change-log.md`) | `tests/unit/ai/test_schema_and_prompts.py` (every prompt version and provider kind is named in the documents; each prompt's digest is pinned) | Implemented (5) as documents for a customer's model risk function to assess; not an independent validation. The customer's model id is not in the documents: it is in every `model.called` audit event |
| ML-08 | Model-drafted text is labelled as such in work notes and the UI | AI Act Art. 13, 50; RMF Govern | `src/l1_agent/ai/advisor.py` (`Interpretation.to_note`), `src/l1_agent/engine/incident_processor.py` (`_model_note`), `src/l1_agent/sop/ingest.py`, `frontend/src/components/IncidentDetail.jsx`, `SopReview.jsx` | `tests/db/test_model_in_runs.py` (`TestTheHandOverSummary`, `TestDraftsFromArticles`), `evals/checks.py` | Implemented (5) in work notes and the run record; an SOP drafted by a model has origin `model_drafted`. The labels in the dashboard have not been checked in a browser |

### Audit and logging

| Id | Control | Maps to | Code | Test | Status |
|---|---|---|---|---|---|
| AU-01 | Append-only audit log with per-tenant hash chain and a verification command | SOC 2 CC7.2; ISO A.8.15, A.5.33; DORA Art. 10; AI Act Art. 12; ASVS Logging; SOX | `src/l1_agent/audit/log.py`, `src/l1_agent/cli.py`, migration `0001` | `tests/db/test_audit_log.py`, `tests/db/test_migrations_and_cli.py` | Implemented (2): chain, `audit verify`, signed checkpoints. Storing checkpoints outside the database is a manual operator step; automatic anchoring and a platform-level chain are not built |
| AU-02 | The audit log records the event classes in section 7.2 of the product brief | SOC 2 CC7.2, CC7.3; ISO A.8.15; AI Act Art. 12 | `src/l1_agent/service/runs.py`, `src/l1_agent/api/app.py`, `src/l1_agent/auth/store.py` | `tests/db/test_runs_durable.py`, `tests/api/test_sign_in.py` | Partial (2): intake, SOP choice, each step start and result, outcome, sign-in success and failure, sign-out, break-glass use, token and role-mapping changes, refused access. Not yet: ITSM writes as their own events, model calls (5), approvals and actions (4), configuration changes (3) |
| AU-03 | One run can be reconstructed from the audit log alone | SOC 2 CC7.3; ISO A.5.28; DORA Art. 17 | `src/l1_agent/audit/log.py` (`events_for`), `l1-agent audit timeline` | `tests/db/test_audit_log.py` (`TestReconstruction`) | Implemented (2) for what AU-02 records today |
| AU-04 | Export to SIEM (Splunk HEC, syslog, OpenTelemetry) with gap detection | SOC 2 CC7.2; ISO A.8.16; DORA Art. 10 | `src/l1_agent/audit/export/` | `tests/audit/test_export_contract.py` | Planned (2) |
| AU-05 | Per-tenant retention, legal hold, write-once evidence store | SOC 2 C1.2; ISO A.5.33, A.8.10; GDPR Art. 5(1)(e) | `src/l1_agent/audit/retention.py`, `evidence_store.py` | `tests/audit/test_retention_and_hold.py` | Planned (2) |
| AU-06 | Operational logs redact message text and exclude secrets and model output | SOC 2 CC6.7; ISO A.8.11, A.8.15; ASVS Logging; LLM02 | `src/l1_agent/utils/logging.py` | `tests/unit/security/test_hardening.py` | Implemented (1). Pattern based |
| AU-07 | Audit viewer restricted to the auditor role; summary mirrored to ITSM work notes | SOC 2 CC6.3; ISO A.8.3 | `src/l1_agent/api/app.py` (`/api/audit/*`) | `tests/api/test_http_access.py` (`TestPermissions`), `tests/api/test_api_data.py` | Partial (2): the audit API needs the `audit.read` permission, held by the auditor role. No viewer screen yet |

### Cryptography, secrets and transport

| Id | Control | Maps to | Code | Test | Status |
|---|---|---|---|---|---|
| CR-01 | Credentials held as references into a secrets manager; never in the database or process environment; rotation without restart | SOC 2 CC6.1, CC6.7; ISO A.5.17, A.8.24; ASVS Configuration | `src/l1_agent/connectors/sdk/secrets.py`, `connectors/sdk/base.py`, `connectors/sdk/http.py`, `connectors/hub.py` | `tests/unit/connectors/test_secrets.py`, `tests/contract/test_servicenow_contract.py` (`TestRotation`), `tests/contract/test_ops_contract.py` (`TestSplunk`) | Partial (3): references and rotation without restart are implemented and tested with file and in-memory backends. The Vault and AWS backends are untested against real services. Bootstrap secrets (database URL, webhook secret, OIDC client secret) are still read once at start-up |
| CR-02 | TLS verification on for all connectors; cannot be disabled outside demo mode | SOC 2 CC6.7; ISO A.8.24, A.8.21; ASVS Communications | `src/l1_agent/connectors/sdk/http.py`, `connectors/sdk/schema.py`, `src/l1_agent/config/settings.py` | `tests/contract/test_servicenow_contract.py` (`TestErrors`), `tests/unit/security/test_hardening.py` | Implemented (3): refused at construction outside demo mode, for stored connections as well as environment settings |
| CR-03 | Mutual TLS and signed, single-use action grants between control plane and runner | SOC 2 CC6.6, CC6.7; ISO A.8.24 | `src/l1_agent/runner/` | `tests/runner/test_grant_verification.py` | Planned (4, 6) |
| CR-04 | Encryption at rest for database, evidence and backups | SOC 2 CC6.1, C1.1; ISO A.8.24, A.8.13; GDPR Art. 32 | `deploy/helm/`, `src/l1_agent/audit/evidence_store.py` | `tests/deploy/test_chart_defaults.py`; platform configuration evidence from the customer | Planned (6) |

### Secure development and supply chain

| Id | Control | Maps to | Code | Test | Status |
|---|---|---|---|---|---|
| SC-01 | Dependencies pinned with hashes | SOC 2 CC8.1; ISO A.8.25, A.8.28; LLM03 | `requirements.lock` | CI job `deps` | Planned (6) |
| SC-02 | SBOM generated per build | ISO A.8.8, A.5.21; DORA Art. 28 | `.github/workflows/` | CI job `sbom` | Planned (6) |
| SC-03 | Secret scanning, static analysis, dependency and image scanning as blocking gates | SOC 2 CC7.1; ISO A.8.8, A.8.29; ASVS Malicious Code | `.github/workflows/ci.yml` | CI jobs `security-scan` | Partial (1): blocking static analysis and a secret scan are defined. Not yet observed running on the hosted runner. Dependency and image scanning Planned (6) |
| SC-04 | Signed images; non-root; read-only root filesystem | SOC 2 CC6.8; ISO A.8.9 | `Dockerfile`, `deploy/helm/` | `tests/deploy/test_chart_defaults.py` | Partial; Planned (6) |
| SC-05 | CI runs on the default branch and on every pull request | SOC 2 CC8.1; ISO A.8.25, A.8.29 | `.github/workflows/ci.yml` | CI itself | Implemented (1): observed passing on the hosted runner for the Phase 1 branch. The Phase 2 workflow adds a PostgreSQL service and has not run there yet |
| SC-06 | No customer names or real personal data in code, fixtures or tests | ISO A.8.33, A.5.34 | `.github/workflows/ci.yml` job `hygiene` | CI job `hygiene` | Implemented (1); needs the `BRANDING_DENYLIST` repository secret. Git history is not covered |

### Resilience

| Id | Control | Maps to | Code | Test | Status |
|---|---|---|---|---|---|
| RS-01 | Durable queue; accepted work survives restart; steps resume without repeating a completed write | SOC 2 A1.2, PI1.3; ISO A.8.14; DORA Art. 11, 12 | `src/l1_agent/service/runs.py`, `src/l1_agent/engine/journal.py` | `tests/db/test_runs_durable.py` (`TestResumeAfterWorkerDeath`), `tests/db/test_worker_killed.py` | Implemented (2) for the read-only steps that exist. Recovery of an interrupted write arrives with write actions (4). A crash during the final ITSM update can repeat the closing work notes |
| RS-02 | Connector health reports honestly; errors are never reported as healthy | SOC 2 CC7.2; ISO A.8.16 | `src/l1_agent/connectors/sdk/base.py` (`health`, `test_connection`), `src/l1_agent/adapters/dynatrace_adapter.py` | `tests/unit/connectors/test_secrets.py` (`TestRotation`), `tests/contract/test_ops_contract.py` (`TestDynatrace`) | Implemented (3) for connector health and for Dynatrace host checks, which now read every page and every host. Against fixtures only |
| RS-03 | Timeouts, safe retries and circuit breakers on every connector | SOC 2 A1.1; ISO A.8.6 | `src/l1_agent/engine/executor.py`, `src/l1_agent/connectors/sdk/http.py`, `src/l1_agent/utils/retry.py` | `tests/contract/test_servicenow_contract.py` (`TestErrors`), `tests/unit/engine/test_executor.py` | Partial (3): timeouts everywhere; ServiceNow retries reads only; breakers are per tenant and connector. The operations connectors still retry every failure, which is safe only because all are reads. Per-capability retry rules Planned (4) |
| RS-04 | Documented backup and restore, including audit chain verification after restore | SOC 2 A1.2, A1.3; ISO A.8.13; DORA Art. 12 | `docs/operator-guide.md` | Restore drill script | Planned (6) |
| RS-05 | Data export for exit and portability: SOPs, configuration, run history, audit log in documented formats | DORA Art. 28(8), 30; UK PRA SS2/21 exit planning; GDPR Art. 20 where applicable | `src/l1_agent/api/export.py` | `tests/api/test_tenant_export.py` | Planned (6) |

## 3. What existed before Phase 1

Verified at commit `db38e1f`, kept for the record. Section 2 shows the current position of each of these rows.

| Row | What exists | Why it is only partial |
|---|---|---|
| AU-06 | `redact_dict` redacts values under sensitive key names in structured log data | Message text is not redacted; model output is logged |
| SC-03 | A bandit job is defined in CI | The workflow never triggers on the default branch, and the medium-severity run ignores failures |
| SC-04 | The Dockerfile creates and switches to a non-root user | No read-only filesystem, no signing, base image not pinned by digest |
| RS-03 | `retry_with_backoff` and `CircuitBreaker` exist and are used by the rule-based executor | Every failure is retried, including validation errors; not used by the other two paths |
| EX-09 | The Autosys adapter runs commands as an argument vector and checks the job name pattern; the Windows share adapter denies when its allow list is empty | Other adapters accept free-form input; the browser adapter allows all when its list is empty |

## 4. Data protection (GDPR and UK GDPR)

These are starting points for the customer's and our privacy reviewers. In the single-tenant deployment the customer is the controller and operates the system; our role depends on the support model. In a hosted deployment we would be a processor.

### 4.1 Data inventory

| Data | Source | Personal data likely | Where held | Sent to a model provider |
|---|---|---|---|---|
| Incident fields: number, descriptions, category, CI, group, priority, state | ITSM | Yes, in free text | Database | Only after redaction, and only fields an advisor task needs |
| Caller name, email, phone | ITSM | Yes | Database, minimised: stored only if a tenant setting enables it | Never |
| Work notes written by the product | Product | Possibly, by quotation | ITSM and database | Not applicable |
| Tool output and evidence | Operations systems | Possibly (user ids in logs) | Evidence store | Only after redaction, for the interpret task |
| User identity: subject, display name, email, groups | Identity provider | Yes | Database | Never |
| Audit events | Product | Pseudonymous actor ids; payloads redacted | Database, SIEM, checkpoint store | Never |
| SOP content | Knowledge sources | Unlikely | Database | For draft generation only |
| Connector credentials | Customer secrets manager | No | Not held; references only | Never |

### 4.2 Lawful basis notes

For the customer's assessment. The processing supports IT operations and security of the customer's own systems; legitimate interests (Art. 6(1)(f)) and, for regulated firms, legal obligation (Art. 6(1)(c)) for operational resilience are the bases most likely to be considered. Special category data is not intended to be processed; free-text tickets can contain it, which is one reason for redaction and for the option to disable model use.

### 4.3 Retention and erasure

- Retention is configurable per tenant per data class (operational records, evidence, audit). Defaults to be proposed in Phase 2 and confirmed by the reviewer.
- Erasure: operational records and the identity mapping table can be erased per data subject. The audit chain holds pseudonymous ids and redacted payloads so that erasing the mapping removes identifiability without breaking the chain. Whether this satisfies Art. 17 in the customer's context needs privacy review; Art. 17(3)(b) and (e) may also apply to audit records.
- Legal hold overrides expiry and is itself audited.

### 4.4 Sub-processors

None in the single-tenant deployment with the model disabled. When a model provider is enabled, that provider is a recipient chosen by the customer in the single-tenant shape, or our sub-processor in the hosted shape. A list is maintained at [`docs/sub-processors.md`](sub-processors.md) and the provider allow list (ML-03) is the technical enforcement.

### 4.5 Data residency

Single-tenant: wherever the customer deploys. Model requests go to the region configured per tenant; requests to other regions are refused in code (ML-03). Hosted: region per tenant, to be designed with that offering.

### 4.6 DPIA template

Delivered as [`docs/dpia-template.md`](dpia-template.md) in Phase 5, a template for the customer's privacy function to complete, with these sections: description of processing and purposes; necessity and proportionality; data inventory (from 4.1); recipients and transfers; risks to individuals (wrongly closed incident affecting a service, disclosure to a model provider, excessive retention, staff monitoring through audit logs); measures (rows AC-05, ML-02, ML-03, AU-05, EX-01); residual risk; consultation and sign-off.

## 5. DORA and UK operational resilience and outsourcing

How the product supports a financial entity's own obligations. The obligations rest with the entity.

| Topic | Reference | Product support | Rows |
|---|---|---|---|
| ICT risk management: protection and prevention | DORA Art. 9 | Access control, change control on SOPs and policy, least-privilege connectors | AC-01 to AC-05, SD-01 to SD-05, EX-04 |
| Detection | DORA Art. 10 | Audit log, SIEM export, honest health | AU-01, AU-04, RS-02 |
| Response and recovery, backup | DORA Art. 11, 12 | Kill switch, durable queue, restore procedure | EX-07, RS-01, RS-04 |
| ICT-related incident management and reporting | DORA Art. 17 to 19 | Timeline reconstruction and evidence export for an incident the entity must classify and report. The product does not classify or report. | AU-03 |
| Third-party risk, contractual provisions, exit | DORA Art. 28 to 30 | SBOM, sub-processor list, data export for exit, documented formats, ability to run without us in the single-tenant shape | SC-02, ML-03, RS-05 |
| UK outsourcing and third-party risk | PRA SS2/21; FCA SYSC 8 | As above; the single-tenant shape keeps data and operation inside the firm | RS-05 |
| UK operational resilience | PRA SS1/21; FCA SYSC 15A | The product is a dependency of important business services only to the extent the firm lets it act; shadow and recommend modes let firms bound that | EX-06, EX-07 |

## 6. SOX IT general controls

| Domain | Product control | Rows |
|---|---|---|
| Change management | SOP versions reviewed and approved by a second person; immutable; linked change record for every production write | SD-01, SD-02, SD-05 |
| Segregation of duties | Author cannot approve; SOP author cannot approve a T2 action from that SOP; enforced on identity | SD-01, SD-04 |
| Logical access | SSO, roles, periodic access review supported by an export of role assignments | AC-01, AC-03, SD-06 |
| Computer operations | Job processing with durable queue, failure handling to a human, audit trail | RS-01, EX-01, AU-01 |

The customer's auditor decides whether the product is in scope, which depends on whether jobs it can restart feed financial reporting.

## 7. AI governance

| Framework | Area | Product support | Rows |
|---|---|---|---|
| NIST AI RMF | Govern | Model inventory, roles, policy for use | ML-07 |
| | Map | Intended use and limitations documented; context of deployment per tenant | ML-07 |
| | Measure | Evaluation suite, injection corpus, per-call audit | ML-05, ML-06 |
| | Manage | Autonomy levels, kill switch, human approval, deterministic fallback | EX-06, EX-07, EX-05, ML-01 |
| ISO/IEC 42001 | AI system life cycle, operation and monitoring, event logging, information for users | As above, plus the labelled model output | ML-05 to ML-08 |
| EU AI Act | Art. 12 record-keeping | Audit log of model calls and decisions | AU-01, ML-05 |
| | Art. 13 and 50 transparency | Documentation of capabilities and limits; labelled model-drafted text | ML-07, ML-08 |
| | Art. 14 human oversight | Approvals, autonomy levels, kill switch, the model cannot act | EX-04 to EX-07, ML-01 |
| | Art. 15 accuracy, robustness, cybersecurity | Fail-closed outcomes, evaluation suite, security controls | EX-01, ML-06 |
| SR 11-7 | Model inventory | `docs/model-risk/inventory.md` | ML-07 |
| | Validation evidence | Evaluation reports per model id and prompt version | ML-06 |
| | Change log and ongoing monitoring | Prompt and model version history; per-call audit | ML-05, ML-07 |

Whether the system falls into a high-risk category under the AI Act is a legal classification that we have not made. The design aims at the Art. 12 to 15 duties regardless, because buyers will ask for them. Whether a customer treats the advisor as a "model" under their SR 11-7 policy is their determination; the documents are prepared so that they can.

## 8. Engineering baseline

| Baseline | Use |
|---|---|
| OWASP ASVS | Level 2 is the target for the API and UI. A chapter-by-chapter self-assessment is a Phase 6 deliverable; rows above cite chapters by name. |
| OWASP Top 10 for LLM Applications | Mapped risk by risk in the [threat model](threat-model.md), section 5. |

## 9. Gap list

Things this matrix does not cover or that remain open even after Phase 6.

1. **No independent assurance.** No SOC 2 report, no ISO 27001 certificate, no penetration test. All are organisational and commercial steps outside this codebase.
2. **Organisational controls.** Policies, personnel security, vendor management, incident response for our own company, business continuity, training. Most SOC 2 and ISO 27001 controls are of this kind and none are addressed here.
3. **Real system behaviour unverified.** Controls are evidenced against mocks and fixtures only. That now includes a control that changes a production scheduler: the force start has never run against Autosys, and the change and approval calls have never run against ServiceNow. Evidence from a real ServiceNow, Autosys, Splunk, Dynatrace or identity provider does not exist.
4. **Platform-level encryption and backup** are provided by the customer's infrastructure in the single-tenant shape. We supply defaults and documentation; the evidence is theirs.
5. **Audit tamper evidence against a database owner** depends on the customer configuring external checkpoints.
6. **Redaction is best effort.** Pattern and classification based; it will miss some content.
7. **Privacy positions need review:** lawful basis, erasure against the audit chain, staff monitoring implications of logging user actions, transfer assessments for model providers.
8. **AI Act classification and SR 11-7 scoping** are not determined.
9. **Accessibility and localisation** of the UI are not assessed.
10. **Hosted control plane** obligations (processor duties, customer-managed keys, regional hosting, our own resilience) are out of scope until that shape is chosen.
11. **Git history** of the current repository contains a customer name and logo. Removing them from the working tree does not address history (decision D2 in the delivery plan).
12. **Framework references** in this document have not been checked by a compliance professional.
