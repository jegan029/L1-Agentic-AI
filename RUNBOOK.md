# L1 Engineer Agent: runbook

How to run and operate the service as it stands after Phase 2. Installation and the demo quick start are in [README.md](README.md); every setting is listed with comments in [.env.example](.env.example).

This is pre-release software. Sign-in has been tested only against identity providers faked in the test suite, and the tool adapters only against mocks. Expect to find differences on first contact with real systems.

## 1. Run it

| Mode | Command | Needs |
|---|---|---|
| One-shot demo | `python -m src.l1_agent.demo` | Nothing. Five sample incidents, all mocks, a throwaway SQLite file. |
| Demo service | `AGENT_DEMO_MODE=true LLM_ENABLED=false python -m src.l1_agent.main` | Nothing. Loopback only; every caller is the demo user. |
| Demo service with real ServiceNow | add `SERVICENOW_LIVE_IN_DEMO=true` and the `SERVICENOW_*` settings | ServiceNow for real; tool adapters stay mocked. |
| Full service | `l1-agent serve` (or `python -m src.l1_agent.cli serve`) | PostgreSQL, a webhook secret, ServiceNow, an identity provider. |
| Extra workers | `l1-agent worker` | The same database. Set `WORKER_EMBEDDED=false` on the API process if workers run only here. |

Containers:

```bash
docker build -t l1-agent .                         # production image
docker build --target demo -t l1-agent-demo .      # one-shot demo image
docker run --rm l1-agent-demo
docker run --rm --env-file .env l1-agent migrate   # with DATABASE_MIGRATION_URL
docker run --rm --env-file .env -p 127.0.0.1:8080:8080 l1-agent
```

The image runs as a non-root user and writes nothing to its filesystem. It listens on all interfaces inside the container, so publish the port deliberately and put TLS termination in front of it. The session cookie is marked Secure: the dashboard must be reached over HTTPS.

## 2. First-time set-up

### 2.1 Database

PostgreSQL 15 or later. Two roles, on purpose:

| Role | Used by | Can do |
|---|---|---|
| Migration role | `l1-agent migrate`, `l1-agent tenant create` | Owns the schema. Needs `CREATEROLE` once, to create the group role `l1_app`, or create that role beforehand. |
| Application role | `serve`, `worker`, every other command | A login that is a member of `l1_app`. Row level security applies to it. |

```sql
-- as a database administrator
CREATE ROLE l1_owner LOGIN PASSWORD '...' CREATEROLE;
CREATE DATABASE l1agent OWNER l1_owner;
CREATE ROLE l1_app_login LOGIN PASSWORD '...';
```

```bash
export DATABASE_MIGRATION_URL=postgresql+asyncpg://l1_owner:...@db:5432/l1agent
export DATABASE_URL=postgresql+asyncpg://l1_app_login:...@db:5432/l1agent
l1-agent migrate
psql -c "GRANT l1_app TO l1_app_login"          # as the migration role or an administrator
l1-agent tenant create --slug default --name "Your organisation"
```

The service refuses to start if `DATABASE_URL` is a superuser, owns the tables, can bypass row level security, can create tenants or can change audit rows. That is deliberate: give it the application role.

Run `l1-agent migrate` before starting a new version. Migrations are forward only.

### 2.2 Access

| Need | Setting or command |
|---|---|
| Webhook signing secret, 32 or more characters | `AGENT_WEBHOOK_SECRET` |
| Sign-in with OpenID Connect | `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URI` (`https://<service>/auth/callback`) |
| Sign-in with SAML | `SAML_IDP_METADATA_PATH`, `SAML_SP_ENTITY_ID`, `SAML_ACS_URL` (`https://<service>/auth/saml/acs`); give the identity provider `/auth/saml/metadata` |
| The first administrator | `l1-agent roles grant --group <idp group> --role tenant_admin` |
| Other roles | Same command, or the administrator uses `PUT /api/role-mappings` |
| A token for a script or the dashboard dev proxy | `l1-agent token create --name ci --permission incidents.read` |
| Emergency access when the identity provider is down | `l1-agent breakglass hash`, then set `BREAK_GLASS_PASSWORD_HASH` |

A user whose groups map to no role cannot sign in. Roles are read at sign-in; a change takes effect at the user's next sign-in. Tokens are shown once, expire (365 days at most) and can carry only read permissions.

The identity provider must send the user's groups: in the `groups` claim of the ID token (`OIDC_GROUPS_CLAIM`) or the `groups` SAML attribute (`SAML_GROUPS_ATTRIBUTE`).

### 2.3 ServiceNow

1. Create a service account with the `itil` role. It reads and updates incidents, reads `sys_user` to find its own id, and reads the knowledge table if you use it as an SOP source.
2. Create the intake assignment group and set `SERVICENOW_ASSIGNMENT_GROUP` to its exact name. The poller takes only incidents that are New and assigned to that group.
3. Create every L2 group named in your routing file. An escalation to a group that does not exist on the instance will not land with a team.
4. Set `SERVICENOW_CLOSE_CODE` to a value in your instance's incident *Resolution code* list. If the update is refused, the run is recorded as `needs_human` with ServiceNow's reason.
5. Check the connection: `pytest tests/live -m live_servicenow -v -s`. This creates incidents tagged `[L1-AGENT-TEST]` and leaves them there; do not run it against production.

Webhook senders must sign each request as described in the README.

**Authentication.** Basic by default. For OAuth 2.0 set `SERVICENOW_AUTH=oauth`, `SERVICENOW_OAUTH_CLIENT_ID` and the client secret (`SERVICENOW_OAUTH_CLIENT_SECRET`, or better `SERVICENOW_OAUTH_CLIENT_SECRET_REF`). With a user name and password also set, the password grant is used; without them, client credentials. The token endpoint defaults to `<instance>/oauth_token.do`. OAuth is covered by contract tests only; it has not run against a real instance.

**Claiming.** Before it does anything to a ticket the agent assigns it to itself (`SERVICENOW_AGENT_USER`, default the integration user) and moves it to In Progress. A ticket that is already assigned to someone else, or no longer open, is left untouched and the run is recorded as `aborted`. The Table API has no conditional update, so a person who takes a ticket in the instant between the agent's check and its update can be overwritten; the agent re-reads the ticket afterwards and stops if it is not the owner. `SERVICENOW_CLAIM=false` switches claiming off.

**Paging.** Lists are read `SERVICENOW_PAGE_SIZE` rows at a time (default 100), up to `SERVICENOW_MAX_PAGES` pages per poll (default 10). The rest is picked up on the next poll.

### 2.4 Connections and credentials

A connection is a connector's settings plus *references* to its credentials. A credential value is never stored in the database and is refused if you try.

| Reference | Where the value is read from |
|---|---|
| `vault:<path>/<field>` | HashiCorp Vault KV v2, under `VAULT_MOUNT` and `VAULT_PATH_PREFIX` |
| `aws:<name>` | AWS SSM Parameter Store, under `AWS_SSM_PREFIX` |
| `file:/absolute/path` | A mounted secret file. `SECRETS_FILE_ROOTS` limits which directories are allowed |
| `env:NAME` | The process environment. Development only: changing it needs a restart |

```bash
l1-agent connectors                      # what each connector can do, and the credentials it needs
l1-agent connection set --connector splunk --config splunk.json --secret token=vault:splunk/token
l1-agent connection test --connector splunk
l1-agent connection list
```

A tenant with no stored connection for a connector uses the deployment's environment settings (`SPLUNK_BASE_URL` and so on), so an install configured through environment variables keeps working. There, `<NAME>_REF` gives a reference in place of the value, for example `SPLUNK_TOKEN_REF=file:/run/secrets/splunk-token`; with `SECRETS_BACKEND=vault` or `aws` the references are derived for you.

**Rotating a credential.** Change it in the secrets backend. Nothing is restarted. Values are cached for 30 seconds; and when a call fails and the backend holds a different value from the one that was used, the connector reads the new value and repeats the call once. For ServiceNow the same happens on a `401`. A credential given as a plain value in the environment (no reference) is read once at start-up and does not follow a rotation.

Changed connection settings are picked up within about 15 seconds. Every change is in the audit log as `connection.updated`, with the references but never a value.

### 2.5 SOPs and knowledge sources

Outside demo mode the service starts with no SOPs. Nothing runs until a version is published.

1. Map identity provider groups to `sop_author` and `sop_approver`. They must be different people for any one version.
2. Add drafts: in the dashboard under **SOP library**, with `l1-agent sop import --file sop.json`, or from a knowledge source.
3. An author submits a draft; an approver reviews, approves and publishes it.

Knowledge sources:

| Source | Settings |
|---|---|
| ServiceNow knowledge base | `KNOWLEDGE_SERVICENOW_KB=true`, `KNOWLEDGE_SERVICENOW_KB_QUERY` (an encoded query, default `workflow_state=published`). Uses the ServiceNow connection |
| Git repository | `KNOWLEDGE_GIT_URL` (https or ssh), `KNOWLEDGE_GIT_REF`, `KNOWLEDGE_GIT_PATH`, `KNOWLEDGE_GIT_TOKEN_REF`. Reads `*.json`, `*.md` and `*.txt`. Needs the `git` command |

`l1-agent knowledge sync` pulls what changed since the last sync; `KNOWLEDGE_SYNC_INTERVAL` (seconds) does it on a timer. Each article revision becomes one draft, once. A file deleted from Git or an article unpublished in ServiceNow does not retire its SOP; retire it in the library.

## 3. Health and monitoring

| Route | Use | Needs |
|---|---|---|
| `GET /health` | Liveness. Status only. | Nothing |
| `GET /api/status` | Mode and a metrics snapshot | `system.read` |
| `GET /metrics` | Prometheus text format, for this process | `metrics.read` |
| `GET /api/audit/verify` | Recompute the audit chain | `audit.read` |

Logs are JSON on standard output. Counters worth alerting on: `http.denied{reason=...}`, `auth.login_failed`, `runs.resumed`, `jobs.infrastructure_errors`.

Queue state is in the `jobs` table: `queued`, `running` (with `locked_until`), `done`, `dead`. A `dead` job's run was recorded as `needs_human`.

## 4. Audit log

```bash
l1-agent audit verify                               # exit status 2 if the chain is broken
l1-agent audit checkpoint >> /secure/checkpoints.jsonl
l1-agent audit verify --checkpoints /secure/checkpoints.jsonl
l1-agent audit timeline --run <run id>              # one run, from audit events alone
```

Schedule `audit checkpoint` and write its output somewhere the database administrator cannot change, such as your SIEM or write-once storage. Without checkpoints, verification cannot tell that the newest events were removed. `AUDIT_CHECKPOINT_KEY` signs them.

After restoring a database backup, run `audit verify --checkpoints` against the checkpoints taken before the backup.

## 5. What to do when

| Symptom | Likely cause | Action |
|---|---|---|
| Exits at start with "the database role is a superuser" or "owns the tables" | `DATABASE_URL` is the migration role | Use the application login (section 2.1). |
| Exits with "Tenant ... does not exist" | No tenant, or the wrong `TENANT_SLUG` | `l1-agent tenant create`. |
| Exits with `SecurityConfigError` or `ConfigError` | A required setting is missing or unsafe | The message names it. |
| Sign-in ends with "sign-in failed" | The reason is not shown to the browser | Look for `auth.login_failed` in the audit log; its payload has the reason. Most often: no group mapped to a role. |
| Signed in, but an action returns `403` | The role lacks the permission, or a missing CSRF token | `GET /api/me` lists roles and permissions. Refusals are audited as `access.denied`. |
| API token returns `401` | Expired, revoked, or for another tenant | `l1-agent token list`. |
| Webhook returns `401` | Bad signature, clock skew over five minutes, or a replayed request | Check the secret and the sender's clock. |
| Requests return `429` | Rate limit | `RATE_LIMIT_PER_MINUTE`, `LOGIN_RATE_LIMIT_PER_MINUTE`. |
| Incidents are never picked up | Not New, or not in the intake group | Check the ticket and `SERVICENOW_ASSIGNMENT_GROUP`. |
| Run ends `aborted` with "Not claimed" | Someone already holds the ticket, or it is no longer open | Nothing to do. The agent wrote nothing to the ticket. |
| Every run ends `escalated` with "No applicable SOP found" | No SOP is published for this tenant | `l1-agent sop list`. Drafts and versions in review are never matched. |
| Run ends `needs_human` with "Adapter '...' not configured" | The SOP calls a connector this tenant has no connection for | `l1-agent connection set`, or `GET /api/connectors` to see the state and any error. |
| A check fails with "credential unavailable" | The reference cannot be read | Check the reference, the backend's reachability and its access policy. |
| A check is refused with "allow list '...' is empty" | The connection lists no permitted targets | Add the hosts, paths or transactions to the connection's settings. |
| Approve returns `403` "The author of a version cannot approve it" | Working as intended | Someone else with `sop_approver` approves. |
| Approve returns `409` "changed since it was reviewed" | The page was open while the version was sent back and resubmitted | Reload and review the current content. |
| Knowledge sync reports "could not read the source" | Source unreachable or credentials refused | The error class is in `knowledge.synced` in the audit log; the cursor has not moved. |
| Runs are accepted but never finish | No worker is running | `WORKER_EMBEDDED=true`, or start `l1-agent worker`. |
| Run ends `escalated` with "Low confidence SOP match" | No SOP reached the threshold | The work note shows the score and which signals were compared. |
| Run ends `needs_human` with "did not finish in N attempts" | A worker died on this run repeatedly | Check worker logs around the run id; the journal shows the last completed step. |
| Run ends `needs_human` with "rejected the Resolved update" | Invalid resolution code, or a business rule | Fix `SERVICENOW_CLOSE_CODE`. |
| Run ends `needs_human` after an adapter error | Target system down, or its circuit breaker is open | Check the target; the breaker resets after `AGENT_CB_RESET_SECONDS`. |

## 6. Data the service keeps

Everything is in PostgreSQL.

| Table | Content | Notes |
|---|---|---|
| `incidents`, `runs`, `run_steps` | Ticket text, the SOP chosen, each step's result and evidence | Contains whatever is in your tickets. No retention control yet. |
| `events` | Progress events for the dashboard stream | Grows; safe to prune by age. No automatic pruning yet. |
| `jobs` | The queue | |
| `audit_events` | The audit chain | Cannot be changed or pruned. Free text is redacted; evidence is a digest. |
| `users`, `sessions`, `api_tokens`, `role_mappings` | Identity provider subject, email and display name; hashes of credentials | No password is stored. |
| `sops`, `sop_versions` | Every SOP version, its author, approver, content hash and source article text | A submitted version cannot be changed and only a draft can be deleted, for any database role. |
| `connections` | Connector settings and credential references | No credential values. |
| `knowledge_sources` | How far each source has been read | |

Back up the database as a whole. There are no files to back up.

## 7. Known operational limits

- One deployment serves one tenant's ServiceNow and identity provider. Operations connectors and knowledge sources are per tenant.
- One connection per connector per tenant: a tenant cannot have two Splunk instances yet.
- The Git source keeps a bare copy of the repository in a cache directory (`KNOWLEDGE_GIT_CACHE_DIR`, default under the temporary directory). It is rebuilt if lost.
- The ticket claim has a short window in which a person's assignment can be overwritten (section 2.3).
- A worker that dies while writing the final ServiceNow update can cause the closing work notes to be posted twice when the run is resumed. The outcome is recorded once.
- If a worker stalls for longer than `WORKER_VISIBILITY_TIMEOUT` without dying, a second worker can pick up its run. Both then replay the same journal and the first conclusion stands, but work notes may repeat.
- The rate limit and the metrics are per process.
- The webhook secret is shared and is rotated by changing the setting and restarting.
- The service does not serve TLS.
