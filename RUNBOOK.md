# L1 and L2 AI-Ops: runbook

How to run and operate the service as it stands after Phase 6. Installing on Kubernetes, the keys, upgrades and backup are in [docs/operator-guide.md](docs/operator-guide.md); the demo quick start is in [README.md](README.md); every setting is listed with comments in [.env.example](.env.example).

This is pre-release software. Sign-in has been tested only against identity providers faked in the test suite, and the tool adapters only against mocks. The Autosys force start has only run against a mock scheduler, and the ServiceNow change and approval calls have run against one developer instance only (reading a person's approval there has not run at all). The runner has only talked to a control plane on the same machine or in one local test cluster. Expect to find differences on first contact with real systems, and start every tenant in shadow mode.

## 1. Run it

| Mode | Command | Needs |
|---|---|---|
| One-shot demo | `python -m src.l1_agent.demo` | Nothing. Eight sample incidents, all mocks, a throwaway SQLite file. The demo tenant is set to act; a real tenant starts in shadow. |
| Demo service | `AGENT_DEMO_MODE=true LLM_ENABLED=false python -m src.l1_agent.main` | Nothing. Loopback only; every caller is the demo user. |
| Demo service with real ServiceNow | add `SERVICENOW_LIVE_IN_DEMO=true` and the `SERVICENOW_*` settings | ServiceNow for real; tool adapters stay mocked. |
| Full service | `l1-agent serve` (or `python -m src.l1_agent.cli serve`) | PostgreSQL, a webhook secret, ServiceNow, an identity provider. |
| Extra workers | `l1-agent worker` | The same database. Set `WORKER_EMBEDDED=false` on the API process if workers run only here. |
| Runner | `l1-agent runner run` | No database. The control plane's address, its own key and the control plane's public keys (section 2.8). |
| The whole shape, as a demo | `docker compose -f deploy/compose/docker-compose.yml up --build` | Docker. PostgreSQL, API, worker, a runner with mock connectors, the dashboard on `127.0.0.1:8088`. |

Containers:

```bash
docker build -t l1-agent .                         # control plane: API, workers, migrations
docker build --target runner -t l1-agent-runner .  # the same code, started as the runner
docker build --target ui -t l1-agent-ui .          # the dashboard, served by nginx
docker build --target demo -t l1-agent-demo .      # one-shot demo image
docker run --rm l1-agent-demo
docker run --rm --env-file .env l1-agent migrate   # with DATABASE_MIGRATION_URL
docker run --rm --env-file .env -p 127.0.0.1:8080:8080 l1-agent
```

The image runs as a non-root user (uid 10001) and writes nothing outside `/tmp`, so it can run with a read-only root filesystem and a writable `/tmp`. Its Python packages are installed from `requirements.lock`, every one at a pinned version with a checked hash. It listens on all interfaces inside the container, so publish the port deliberately. Put TLS termination in front of it, or give it a certificate with `AGENT_TLS_CERT_FILE` and `AGENT_TLS_KEY_FILE`. The session cookie is marked Secure: the dashboard must be reached over HTTPS.

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

The connection can be given in the environment (`SERVICENOW_*`, as below) or entered on the dashboard's Set-up page, which stores it like any other connection: settings plus a reference to the password. A stored connection wins over the environment's, is picked up by every process within about 15 seconds, and if it is broken the service does not fall back to the environment's: it takes no incident until the stored one is fixed or removed. With no connection at all the service starts, the webhook answers 503 and the poller does nothing. `l1-agent connection test --connector servicenow` signs in and makes the two reads intake depends on.

1. Create a service account with the `itil` role. It reads and updates incidents, reads `sys_user` to find its own id, and reads the knowledge table if you use it as an SOP source.
2. Create the intake assignment group and set `SERVICENOW_ASSIGNMENT_GROUP` to its exact name. The poller takes only incidents that are New and assigned to that group.
3. Create every L2 group named in your routing file. An escalation to a group that does not exist on the instance will not land with a team.
4. Set `SERVICENOW_CLOSE_CODE` to a value in your instance's incident *Resolution code* list. If the update is refused, the run is recorded as `needs_human` with ServiceNow's reason.
5. Check the connection: `pytest tests/live -m live_servicenow -v -s`. This creates incidents tagged `[L1-AGENT-TEST]` and leaves them there; do not run it against production.

Webhook senders must sign each request as described in the README.

**Authentication.** Basic by default. For OAuth 2.0 set `SERVICENOW_AUTH=oauth`, `SERVICENOW_OAUTH_CLIENT_ID` and the client secret (`SERVICENOW_OAUTH_CLIENT_SECRET`, or better `SERVICENOW_OAUTH_CLIENT_SECRET_REF`). With a user name and password also set, the password grant is used; without them, client credentials. The token endpoint defaults to `<instance>/oauth_token.do`. OAuth is covered by contract tests only; it has not run against a real instance.

**Claiming.** Before it does anything to a ticket the agent assigns it to itself (`SERVICENOW_AGENT_USER`, default the integration user) and moves it to In Progress. A ticket that is already assigned to someone else, or no longer open, is left untouched and the run is recorded as `aborted`. The Table API has no conditional update, so a person who takes a ticket in the instant between the agent's check and its update can be overwritten; the agent re-reads the ticket afterwards and stops if it is not the owner. When the agent escalates, it clears the assignment in the same update that changes the group: a stock instance refuses a group change on a ticket that is still assigned. `SERVICENOW_CLAIM=false` switches claiming off.

**Paging.** Lists are read `SERVICENOW_PAGE_SIZE` rows at a time (default 100), up to `SERVICENOW_MAX_PAGES` pages per poll (default 10). The rest is picked up on the next poll. A page counts against the ceiling even when access controls leave it empty, so for a table where the integration user can read only a small share of the rows, raise the page size.

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

The dashboard's **Set-up** page does the same: it shows each connector, what it can do, whether it is connected and from where, and a form built from the connector's own settings schema. *Save and test* stores the connection and then tests it from where its calls will be made. `l1-agent onboarding` prints the set-up steps and what is missing; it exits 0 when every required step is done and 3 otherwise.

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

### 2.6 Letting the agent make changes

A new tenant is in **shadow**: at an `ACTION` step the agent records what it would have done and changes nothing. Leave it there until the shadow record looks right. The design is in [ADR 0008](docs/adr/0008-actions-policy-and-approvals.md).

**Before the first change**

1. **ServiceNow.** Create a standard change template for each SOP that has an `ACTION` step, with exactly the name in the SOP's `change_template`. The integration user must be able to create a standard change from it and to update that change. With no change record, the agent does not act. A stock instance will not move a change out of New unless it has an assignment group: either put one in the template's values, or set `SERVICENOW_CHANGE_ASSIGNMENT_GROUP` and the agent puts that group on each change it opens. Without one the agent still acts, and the change is left open with a note (`change.close_failed` in the audit log names the missing condition). A standard change shows *Approved* in ServiceNow from the moment it is created; that is the template's pre-approval, not a person's decision on this action, and the agent does not read it.
2. **Autosys.** Set the connection's `force_start_jobs` (or `AUTOSYS_FORCE_START_JOBS`) to the jobs that may be restarted: exact names or patterns such as `NIGHTLY_*`. An empty list means none. `sendevent` must be on the agent host (`AUTOSYS_SENDEVENT_PATH`), run as an account that may send `FORCE_STARTJOB` to those jobs and nothing more.
3. **Roles.** Map a group to `approver`. Keep it separate from `tenant_admin`: the person who sets how far the agent may go does not also approve what it does.

To check the ServiceNow side on a developer or sub-production instance, set `LIVE_SERVICENOW_CHANGE_TEMPLATE` to the template's name and run `pytest tests/live/test_servicenow_changes_live.py -m live_servicenow -v -s`. It creates tagged change records and leaves them there. This test has not yet been run against any instance.

**Raising the level**

```bash
l1-agent policy show
l1-agent policy set --autonomy approval --rules rules.json
```

| Level | What an `ACTION` step does |
|---|---|
| `shadow` | Records the action it would take. Nothing changes. |
| `recommend` | Posts the proposed action to the ticket and escalates. |
| `approval` | Opens the change, asks for approval, acts when it is given. |
| `automatic` | Acts without a person for tier 1 actions on the `automatic` list. Everything else asks. |

Three levels apply and the lowest wins: the tenant's, the one in `sop_autonomy` for the SOP version that is running, and the deployment ceiling `ACTIONS_MAX_AUTONOMY`. `sop_autonomy` names the version's content hash, so **a newly published version is in shadow until you list its hash**. The hash is on the version's page in the SOP library and in `l1-agent sop list`.

```json
{
  "sop_autonomy": {"SOP-AUTOSYS-002": {"level": "automatic", "content_hash": "<sha256 of the published version>"}},
  "automatic":    [{"sop_id": "SOP-AUTOSYS-002", "capability": "autosys.job.force_start", "targets": ["NIGHTLY_*"]}],
  "raise_tier":   [{"capability": "autosys.job.force_start", "targets": ["PAYROLL_*", "GL_*"]}],
  "freeze":       [{"start": "2026-12-20T00:00:00Z", "end": "2027-01-05T00:00:00Z", "reason": "year end"}],
  "hours":        {"days": [0, 1, 2, 3, 4], "start": "06:00", "end": "22:00"},
  "rate":         {"max_actions": 10, "per_minutes": 60},
  "approval":     {"ttl_minutes": 60, "execute_within_minutes": 15, "channels": ["ui"], "servicenow_group": ""}
}
```

- `raise_tier` makes the named targets tier 2. **Tier 2 always needs an approval**, from someone other than the SOP's author. Nothing lowers a tier.
- `hours` and `freeze` are in UTC. `days` counts Monday as 0.
- Invalid rules are refused whole, with every problem listed; the previous rules stay.
- `ACTIONS_MAX_TIER=T0` on a deployment means it never makes a change, whatever any tenant sets.

**Approvals**

- In the dashboard under **Actions**: the approver sees the capability, the parameters, the SOP version, the change number and the expiry, and approves or rejects exactly that.
- In ServiceNow, if `approval.channels` includes `servicenow`: the agent adds an approval request on the change for each active member of `servicenow_group` (up to 25) and checks for a decision every `ACTIONS_SERVICENOW_POLL_SECONDS`. One rejection rejects; otherwise one approval approves. The integration user needs to read `sys_user_grmember` and to create and read `sysapproval_approver`. For tier 2 the approver's email must be known and differ from the SOP author's; if not, the approval is not accepted and the run escalates.
- A request nobody answers expires after `ttl_minutes` and the run escalates. An approval must be acted on within `execute_within_minutes`.

**The kill switch**

```bash
l1-agent killswitch on --reason "runaway restarts"      # this tenant
l1-agent killswitch off
l1-agent killswitch on --global --reason "bad release"  # every tenant; needs DATABASE_MIGRATION_URL
l1-agent killswitch status
```

Or **Actions → Stop all changes** in the dashboard. An operator or tenant administrator can engage the tenant's switch; only a tenant administrator can release it. A third switch is local to a process: while the file named by `ACTIONS_KILL_SWITCH_FILE` exists, that process makes no change. With any switch on, runs still read and diagnose; a run that reaches a change ends `aborted` and the ticket goes to a person. The switches are read from the database each time, so they take effect on the next change, not after a delay.

### 2.7 Using a model

The agent works without a model, and a new tenant uses none. Switch it on only after your own review of what will be sent and to whom. The design is in [ADR 0009](docs/adr/0009-model-use.md) and the description for a model risk function in [docs/model-risk/](docs/model-risk/).

**The provider (per deployment)**

```bash
LLM_ENABLED=true
LLM_PROVIDER=openai_compatible        # or anthropic
LLM_ENDPOINT=https://models.example/v1
LLM_MODEL=vendor/model-name
LLM_API_KEY_REF=vault:secret/data/l1-agent#llm_api_key   # read for each request
LLM_REGION=eu                         # what you state about where the endpoint processes data
LLM_RETENTION_DAYS=0                  # what you state about how long it keeps prompts
LLM_INPUT_PRICE_PER_MTOK=3            # for cost budgets; leave unset for token budgets only
LLM_OUTPUT_PRICE_PER_MTOK=15
```

The endpoint must be `https`. Region and retention are your declaration: the agent checks each tenant's rule against them and cannot verify them with the provider. Take them from your contract with the provider.

**The tenant's policy**

```bash
l1-agent model show
l1-agent model set --on --rules model-rules.json
l1-agent model set --off
l1-agent model usage
```

```json
{
  "providers": ["openai_compatible"],
  "tasks": ["rank", "interpret"],
  "regions": ["eu"],
  "max_retention_days": 30,
  "budgets": {"incident_tokens": 20000, "daily_tokens": 1000000, "incident_cost_usd": 0.05, "daily_cost_usd": 20}
}
```

| Task | What the model is asked | What it can affect |
|---|---|---|
| `rank` | Which SOP candidate fits best | Which of two read-only procedures runs, when both already fit the ticket |
| `extract` | Where in the ticket a value is | Whether a read-only procedure can run, for a parameter the SOP author marked with `"advisor": true` |
| `interpret` | A hand-over summary of a run that did not resolve | A note on the ticket, labelled as model-drafted |
| `draft` | Steps from a prose knowledge article | A draft SOP, marked as model-drafted, that two people must still pass |

- Start with `rank` and `interpret`. Add `extract` and `draft` when you have looked at what they produce.
- With `regions` set, a provider that states no region is refused. With `max_retention_days` set, a provider that states no retention is refused.
- A change takes effect on the next request. Switching off needs no restart.
- Past a budget the request is not sent and the run goes on without the model.

**What is sent**

Ticket text, check results and article text, after redaction. Secrets and personal data of a recognised shape are replaced with placeholders. Redaction is pattern matching: it does not find a person's name in running text (other than the ticket's caller), a postal address, or a secret that looks like ordinary words. If your tickets carry such data and that is not acceptable, do not enable the model.

**Checking a model before you rely on it**

```bash
python -m evals.run            # stand-in models; no cost; also runs in CI
python -m evals.run --live     # your configured model; about forty requests
```

The live run needs a source checkout: the `evals/` directory is not in the container image. Record the result in `docs/model-risk/change-log.md` whenever the model id, the provider or a prompt version changes.

### 2.8 The runner

With `RUNNER_MODE=remote` on the API and the workers, no call to Splunk, Autosys or any other operations system is made by them. A runner makes each one ([ADR 0010](docs/adr/0010-runner-protocol.md)). `RUNNER_MODE=embedded` (the default) makes the calls in the worker, as before.

```bash
l1-agent runner keygen --out dispatch.key     # for the control plane: signs calls
l1-agent runner keygen --out grant.key        # for the workers: signs grants for writes
l1-agent runner keygen --out runner.key       # on the runner's machine
l1-agent runner enrol --name runner-1 --public-key-file runner.key.pub
l1-agent runner list                          # connected, idle or revoked
l1-agent runner revoke --id <id>              # refused from its next request
```

| Process | Settings |
|---|---|
| API | `RUNNER_MODE=remote`, `RUNNER_DISPATCH_KEY_REF=file:/path/dispatch.key` |
| Worker | The same, and `ACTIONS_GRANT_KEY_REF=file:/path/grant.key`. Give the grant key to workers only |
| Runner | `RUNNER_CONTROL_PLANE_URL=https://...`, `RUNNER_PRIVATE_KEY_REF=file:/path/runner.key`, `RUNNER_DISPATCH_PUBLIC_KEY`, `RUNNER_GRANT_PUBLIC_KEY` (or each with `_FILE`) |

A runner that was not given `RUNNER_ID` is recognised by its key. That is enough while one deployment serves one tenant.

**The runner's own limits.** Set where the runner runs; nothing in the control plane can change them.

| Setting | Default | Meaning |
|---|---|---|
| `RUNNER_MAX_TIER` | `T0` | The highest tier it will carry out. `T0` is reads only |
| `RUNNER_GRANT_PUBLIC_KEY` | empty | Without it the runner refuses every write |
| `RUNNER_TARGETS_FILE` | none | JSON: connector, then allow list setting, then patterns. A write is made only to a target listed here |
| `RUNNER_CAPABILITIES` | `*` | Patterns of capability ids it will carry out |
| `RUNNER_SECRET_REF_PREFIXES` | empty | Where a credential reference sent by the control plane may point. Empty: none is followed |
| `RUNNER_CONNECTIONS_FILE` | none | Connections the runner holds itself. For a connector listed here, what the control plane sends is ignored |
| `RUNNER_KILL_SWITCH_FILE` | none | While the file exists the runner makes no write |

A refusal by the runner reaches the run as the step's error text, for example *this runner has no list of its own for 'force_start_jobs', so it makes no write to any target*. Nothing was changed.

## 3. Health and monitoring

| Route | Use | Needs |
|---|---|---|
| `GET /health` | Liveness. Status only. | Nothing |
| `GET /api/status` | Mode and a metrics snapshot | `system.read` |
| `GET /metrics` | Prometheus text format, for this process | `metrics.read` |
| `GET /api/audit/verify` | Recompute the audit chain | `audit.read` |

Logs are JSON on standard output. Counters worth alerting on: `http.denied{reason=...}`, `auth.login_failed`, `runs.resumed`, `jobs.infrastructure_errors`, and with a runner `runner.calls_unanswered` and `actions.outcome_unknown`.

A worker and a runner have no listener and so no health route. A runner is healthy when the control plane has heard from it in the last 90 seconds: `l1-agent runner list`, `GET /api/runners`, or the Set-up page.

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
| Run ends `aborted` with "kill switch is on" | A kill switch was on when the run reached a change | `l1-agent killswitch status`. The ticket has been routed to a person. |
| An action is always "Shadow: would have run" | The tenant, or this SOP version, is in shadow | `l1-agent policy show`. A new SOP version needs its hash in `sop_autonomy`. |
| Action denied: `change_record_unavailable` | No standard change template with the SOP's `change_template` name, or the integration user may not create one | Create the template; check the user's rights. |
| Action denied: `not_on_allow_list` | The job is not on the connection's `force_start_jobs`, or the list is empty | Add it to the connection. |
| Action denied: `above_deployment_ceiling`, `change_freeze`, `outside_permitted_hours`, `rate_limit` | The rule named | `l1-agent policy show`; `ACTIONS_MAX_TIER`. |
| A run sits in "waiting for approval" | Nobody has decided | **Actions** in the dashboard, or the change in ServiceNow. It escalates when the request expires. |
| Approve returns `403` "author of the SOP cannot approve a T2 action" | Working as intended | Another approver decides. |
| Approve returns `409` | Already decided, expired, or not the action that was shown | Reload the page. |
| Run ends `escalated` with "ran but was not verified" | The job was started and did not reach `SU` in the wait the SOP allows, or failed again | The change record is closed as unsuccessful. L2 has both status reads. |
| Run ends `needs_human` with "Whether it happened is not known" | A worker died between sending the write and recording its result, or a runner took the write and never answered | Check the scheduler. The agent did not send it again. |
| `change.close_failed` in the audit log | The instance refused a state change when closing the change. The entry names the condition the instance says is unmet, most often "Assignment group is not empty" | Set `SERVICENOW_CHANGE_ASSIGNMENT_GROUP` or add a group to the template. Close this change by hand. The run's outcome is unaffected. |
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
| The model seems to do nothing | The tenant has no model policy, the task is not listed, or the provider fails a region or retention rule | `l1-agent model show`; the Actions page says why each task is not used. |
| `model.budget_exceeded` in the audit log | A run or the day reached its token or cost budget | Nothing to do: the run went on without the model. Raise the budget if it is too low. |
| Many `invalid` outcomes in `l1-agent model usage` | The model's answers do not fit the schema, or are cut short. A model that reasons before answering needs a large output allowance | Run `python -m evals.run --live`. Tell us the model id. |
| Many `error` outcomes | The provider is failing or timing out | Check the provider. Raise `LLM_TIMEOUT_SECONDS` if it is slow. |
| Run ends `needs_human` after an adapter error | Target system down, or its circuit breaker is open | Check the target; the breaker resets after `AGENT_CB_RESET_SECONDS`. |
| A step ends "No runner took the call in time" | `RUNNER_MODE=remote` and no runner is connected | `l1-agent runner list` shows when each last asked for work. The runner's own log says why it cannot reach the control plane: a 401 means its key is not enrolled, was revoked, or its clock is more than a minute out. The run has gone to a person and nothing was changed. |
| A step is refused with "this runner ..." | The runner's own limits (section 2.8) | Raise `RUNNER_MAX_TIER`, list the target in `RUNNER_TARGETS_FILE`, or add the reference's prefix to `RUNNER_SECRET_REF_PREFIXES`, where the runner runs. |
| A write ends "The grant was not issued by the policy engine" | The runner's `RUNNER_GRANT_PUBLIC_KEY` is not the public half of the workers' grant key, or a worker has no `ACTIONS_GRANT_KEY_REF` | `GET /api/runners` shows the public keys a runner should be given. Nothing was changed. |
| The webhook returns `503` | No usable ServiceNow connection | Section 2.3. `l1-agent onboarding` says why. |

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
| `policies` | Autonomy level, rules and the tenant kill switch | |
| `actions` | Every policy decision: capability, parameters, hash, tier, approver, change number, result | Cannot be deleted. What was decided and approved cannot be changed, for any database role. |
| `platform_flags` | The global kill switch | The only table besides `tenants` with no tenant. The application can read it and cannot write it. |
| `knowledge_sources` | How far each source has been read | |
| `model_policies` | Whether the tenant uses a model, with which providers, tasks and budgets | |
| `model_calls` | Each model request: task, model id, prompt version, digests, token counts, cost, outcome | No prompt or answer text. Cannot be changed or deleted. Grows with use; no pruning yet. |

Back up the database as a whole. There are no files to back up. The procedure, a restore drill and the export for leaving with your data are in [docs/operator-guide.md](docs/operator-guide.md), section 7.

## 7. Known operational limits

- One deployment serves one tenant's ServiceNow and identity provider. Operations connectors and knowledge sources are per tenant.
- One connection per connector per tenant: a tenant cannot have two Splunk instances yet.
- The Git source keeps a bare copy of the repository in a cache directory (`KNOWLEDGE_GIT_CACHE_DIR`, default under the temporary directory). It is rebuilt if lost.
- The ticket claim has a short window in which a person's assignment can be overwritten (section 2.3).
- A worker that dies while writing the final ServiceNow update can cause the closing work notes to be posted twice when the run is resumed. The outcome is recorded once.
- If a worker stalls for longer than `WORKER_VISIBILITY_TIMEOUT` without dying, a second worker can pick up its run. Both then replay the same journal and the first conclusion stands, but work notes may repeat.
- The rate limit and the metrics are per process.
- A run waiting for approval holds its incident's one open run until it is decided or expires.
- The same change is made at most once per incident. If a restart is needed twice, the second is a person's.
- When a change record cannot be closed automatically it is left open with a note.
- ServiceNow approvals are polled; a decision takes up to `ACTIONS_SERVICENOW_POLL_SECONDS` to be seen.
- One model provider per deployment.
- The daily model budget is checked without a lock: several workers at once can pass it by the size of the requests in flight.
- The webhook secret is shared and is rotated by changing the setting and restarting.
- The service serves TLS only if it is given a certificate (`AGENT_TLS_CERT_FILE`, `AGENT_TLS_KEY_FILE`).
- With a runner: every runner sees every call for its tenant; a call waits at most its capability's timeout plus 20 seconds for a runner and is then withdrawn; the signing keys are read at start-up, so replacing one needs a restart of everything that holds either half.
- A step that could not reach a runner ends as an error and the run goes to a person. It is not retried.
- A write the runner took and never answered is not sent again. The target is read; if it does not show the result, the action is recorded as not known and the run goes to a person.
- Nothing is ever deleted, except rows of the two queues.
