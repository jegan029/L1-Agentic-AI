# Operator guide: installing and running on Kubernetes

For the person who installs L1 and L2 AI-Ops in their own cluster and keeps it running. It covers the Helm chart, the keys, the runner, upgrades, backup and restore. Day-to-day operation of the product itself (policies, SOPs, the audit log, what to do when something is wrong) is in [RUNBOOK.md](../RUNBOOK.md). What the product does and does not protect against is in the [security guide](security-guide.md).

**What has and has not been run.** The chart was installed in a local test cluster (kind) with the script in `tools/ci/kind_install.sh`: PostgreSQL in the cluster, the install job, every pod ready, `helm test`, the runner connected over TLS, an upgrade, and a restore drill. It has not been installed in a managed Kubernetes service, behind a real ingress controller, against a managed database or behind a connection pooler. No signed image has been published yet, so steps that mention verifying a signature describe what will be possible, not something that has been done.

## 1. What gets deployed

| Component | What it is | Listens | Holds |
|---|---|---|---|
| API | The HTTP API, the ServiceNow poller, the knowledge sync | Port 8080 | Database (application role), webhook secret, the dispatch key |
| Worker | Takes runs from the queue and executes SOPs | Nothing | Database (application role), the dispatch key, the grant key |
| Runner | Makes every call to Splunk, Autosys and the other operations systems | Nothing | Its own key, the connectors' credentials. No database |
| Dashboard | The web pages, and a pass-through to the API so both share one origin | Port 8080 | Nothing |
| Install job | Runs before each install and upgrade: migrations, the tenant, the runner's enrolment | Nothing | Database (migration role), for the length of the job |

One release serves one tenant ([ADR 0003](adr/0003-deployment-shape.md)). The control plane and the runner are in one chart because in this shape they are deployed together; the runner can equally be run elsewhere from the same image.

Every container runs as a non-root user with a read-only root filesystem, no added capabilities and no Kubernetes API token. The chart refuses values that would change that.

## 2. Before you install

You need: Kubernetes 1.25 or later, Helm 3, PostgreSQL 15 or later, the two images in a registry your cluster can pull from, and a way to serve the dashboard over TLS (an ingress controller, or your own).

### 2.1 Images

Build them from the repository, or pull them from where they are published:

```bash
docker build -t <registry>/l1-and-l2-ai-ops:<version> .
docker build --target ui -t <registry>/l1-and-l2-ai-ops-ui:<version> .
```

The control plane and the runner use the same image. Deploy by digest (`image.digest`, `ui.image.digest`) where you can: a tag can be moved, a digest cannot.

Once releases are published with signatures ([.github/workflows/release.yml](../.github/workflows/release.yml)), verify an image before you deploy it:

```bash
cosign verify <image>@<digest> \
  --certificate-identity-regexp '^https://github.com/<owner>/<repo>/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

### 2.2 Database

Two roles. The migration role owns the schema and is used only by the install job. The application role is what the product runs as: row level security applies to it, it cannot create a tenant and it cannot change the audit log. The product refuses to start as the wrong one.

```sql
CREATE ROLE l1_app NOLOGIN;                                  -- the group the migrations grant to
CREATE ROLE aiops_app LOGIN PASSWORD '...' IN ROLE l1_app;   -- the application role
-- The migration role is the owner of the database, or a role with CREATE on its schema.
```

Turn on encryption at rest and encrypted backups in the database service. The product does not encrypt anything itself (controls matrix, CR-04).

```bash
kubectl -n aiops create secret generic aiops-database \
  --from-literal=url='postgresql+asyncpg://aiops_app:...@db.internal:5432/aiops' \
  --from-literal=migrationUrl='postgresql+asyncpg://aiops_owner:...@db.internal:5432/aiops'
```

### 2.3 Keys

Three key pairs ([ADR 0010](adr/0010-runner-protocol.md)). Make each one where its private half will be kept, and move only the public half.

```bash
l1-agent runner keygen --out dispatch.key   # the control plane signs calls to the runner
l1-agent runner keygen --out grant.key      # the policy engine signs grants for writes
l1-agent runner keygen --out runner.key     # the runner signs its requests
```

Each command writes the private key to the file named and the public key beside it (`.pub`). It never overwrites a file. The `l1-agent` command is in the image: `docker run --rm -v "$PWD:/keys" --user "$(id -u)" <image> runner keygen --out /keys/dispatch.key`.

```bash
kubectl -n aiops create secret generic aiops-dispatch-key --from-file=dispatch.key
kubectl -n aiops create secret generic aiops-grant-key    --from-file=grant.key
kubectl -n aiops create secret generic aiops-runner-key   --from-file=runner.key
```

Then delete the private key files from wherever you made them.

| Key | Who gets the private half | Who gets the public half |
|---|---|---|
| `dispatch.key` | API and workers (`controlPlane.dispatchKeySecret`) | The runner (`runner.dispatchPublicKey`) |
| `grant.key` | Workers only (`controlPlane.grantKeySecret`) | The runner (`runner.grantPublicKey`) |
| `runner.key` | The runner (`runner.keySecret`) | The control plane, by enrolment (`runner.publicKey`) |

Leave out the grant key and its public half and no write can ever be made, whatever any policy says. That is a reasonable way to start.

### 2.4 The other secrets

```bash
kubectl -n aiops create secret generic aiops-secrets \
  --from-literal=webhookSecret="$(openssl rand -base64 48)"
```

`webhookSecret` is required: ServiceNow signs each webhook with it. Optional keys in the same Secret, each switched on with `secrets.present.<name>: true`: `auditCheckpointKey` (to sign audit checkpoints), `oidcClientSecret`, `breakGlassPasswordHash` (from `l1-agent breakglass hash`).

Credentials for the systems the runner talks to go in one more Secret, mounted in the runner at `/run/secrets/connectors` (`runner.connectorSecret`). A connection then refers to one as `file:/run/secrets/connectors/<key>`. If you use Vault or AWS instead, set `runner.config` (`SECRETS_BACKEND`, `VAULT_ADDR`, ...) and add the prefix to `runner.extraSecretRefPrefixes`.

### 2.5 TLS between the runner and the API

The runner connects over TLS or not at all. Two ways:

- **The API serves HTTPS itself.** Create a `kubernetes.io/tls` Secret with a certificate for `<release>-l1-and-l2-ai-ops-api.<namespace>.svc`, set `api.tls.secretName`, and give the runner the CA (`runner.caSecret`, key `ca.crt`). The runner then connects straight to the API's service. This is what the install test does.
- **TLS ends at your ingress.** Leave `api.tls.secretName` empty and set `runner.controlPlaneUrl` to the public `https://` address. Traffic between the ingress and the API pod is then plain HTTP inside the cluster; use a service mesh if that is not acceptable to you.

The chart does not render if neither is set.

## 3. Install

A values file with the least you have to set:

```yaml
image:
  repository: registry.example.com/l1-and-l2-ai-ops
  digest: sha256:...
ui:
  image:
    repository: registry.example.com/l1-and-l2-ai-ops-ui
    digest: sha256:...
tenant:
  slug: operations
database:
  existingSecret: aiops-database
secrets:
  existingSecret: aiops-secrets
controlPlane:
  dispatchKeySecret: aiops-dispatch-key
  grantKeySecret: aiops-grant-key
api:
  tls:
    secretName: aiops-api-tls
runner:
  keySecret: aiops-runner-key
  caSecret: aiops-api-tls
  publicKey: "<contents of runner.key.pub>"
  dispatchPublicKey: "<contents of dispatch.key.pub>"
  grantPublicKey: "<contents of grant.key.pub>"
config:
  OIDC_ISSUER: https://login.example.com
  OIDC_CLIENT_ID: l1-and-l2-ai-ops
  OIDC_REDIRECT_URI: https://aiops.example.com/auth/callback
ingress:
  enabled: true
  className: nginx
  host: aiops.example.com
  tls:
    secretName: aiops-ingress-tls
networkPolicy:
  ingressFrom:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: ingress-nginx}
```

```bash
helm install aiops deploy/helm/l1-and-l2-ai-ops -n aiops -f values.yaml --wait
helm test aiops -n aiops --logs
```

Before anything else starts, the install job applies the schema migrations with the migration role, creates the tenant if it does not exist, and enrols the runner's public key. It runs again on every upgrade and changes nothing that is already there.

`helm test` starts a pod with the API's image and security settings. It fails unless the container is running as a non-root user, cannot write to its root filesystem, can write to `/tmp`, and the API answers.

Keep the values file in version control. Settings given this way are not recorded in the product's audit log; their history is the file's.

## 4. After the install

```bash
kubectl -n aiops exec deploy/aiops-l1-and-l2-ai-ops-api -- python -m src.l1_agent.cli onboarding
```

prints what is and is not set up, in the order to do it. The dashboard's **Set-up** page shows the same list and is where most of it is done:

1. **Sign-in and roles.** Give `OIDC_*` or `SAML_*` in `config`, then map each identity provider group to a role: `l1-agent roles grant --group <group> --role tenant_admin` from an API pod, or the API (RUNBOOK 2.2). The dashboard has no screen for role mappings. The break-glass account is for when the identity provider is unavailable.
2. **ServiceNow.** Enter the instance, the integration user, the intake group and a reference to the password. Until this is done the product takes no incident: the webhook answers 503. It is used from the moment it is saved, in every pod, within about fifteen seconds.
3. **Runner.** Should already show as connected. If not, see section 8.
4. **Operations connectors.** Each takes its settings and a reference to its credential, never the credential. *Save and test* makes the test from the runner, which is where the calls will be made from.
5. **An SOP source** (optional), **a published SOP**, **the autonomy level.** A new tenant is in shadow and stays there until an administrator says otherwise.

The mainframe and browser connectors are previews: read only, verified against mocks alone, and the browser connector's Chrome is not in the image.

## 5. Letting the runner make changes

Out of the box the runner reads and does nothing else. For it to make a write, all of these have to be true, and each is set by a different person on purpose:

| What | Where | Who |
|---|---|---|
| The runner may go as high as the capability's tier | `runner.maxTier: T1` | You |
| The runner can check a grant | `runner.grantPublicKey` | You |
| The target is on the runner's own list | `runner.targets: {autosys: {force_start_jobs: ["NIGHTLY_*"]}}` | You |
| The target is on the connection's list | The connection's `force_start_jobs`, in Set-up | Tenant administrator |
| Policy allows it, or an approver approves it | Actions page, `l1-agent policy set` | Tenant administrator, approver |
| The SOP that asks for it is published | SOP library | Author and a different approver |

The first three are yours alone. Nothing in the control plane can change them: they are environment variables and a mounted file in the runner's pod.

To stop the runner writing at once, without touching the control plane: set `runner.maxTier: T0` and upgrade, or mount a file and name it in `runner.config.RUNNER_KILL_SWITCH_FILE`. To stop everything for the tenant, use the kill switch (RUNBOOK 2.6).

To keep a connection's address and settings out of the control plane's hands, put it under `runner.connections`. What the control plane holds for that connector is then ignored by the runner.

The chart places no limit on where the runner may connect. Add egress rules for it that allow the control plane, your secrets manager and the systems it is meant to reach, and nothing else.

## 6. Upgrades

```bash
helm upgrade aiops deploy/helm/l1-and-l2-ai-ops -n aiops -f values.yaml --wait
```

- Migrations are forward only. There is no downgrade: to go back, restore the database from the backup you took first (section 7).
- The install job migrates before the new pods start. Old pods keep running against the new schema until they are replaced, so read the release notes for a migration that an old version could not live with.
- A run in progress when its worker stops is picked up by another worker and resumes from its last recorded step. A write that was in flight is not sent again: the target is read to find out what happened (RUNBOOK 5).
- Calls waiting for a runner while it restarts are withdrawn after their timeout and the step ends as an error. Restart the runner when it is quiet if you can.

## 7. Backup and restore

Everything the product keeps is in PostgreSQL. There is no other state: no files, no volumes.

**Back up** with your database service's own backups, or:

```bash
pg_dump --format=custom --file=aiops.dump "$OWNER_DATABASE_URL"
```

After each backup, take an audit checkpoint and store it somewhere the database administrator cannot change (RUNBOOK 4). A checkpoint is what lets you show later that a restored log was not cut short.

```bash
kubectl -n aiops exec deploy/aiops-l1-and-l2-ai-ops-api -- python -m src.l1_agent.cli audit checkpoint >> checkpoints.jsonl
```

**Restore**, to a new database:

```bash
createdb aiops_restored
pg_restore --dbname=aiops_restored --exit-on-error aiops.dump
```

The two roles must exist in the target server first (section 2.2). Then, before you point the product at it, check the copy as the application role:

```bash
DATABASE_URL='postgresql+asyncpg://aiops_app:...@db.internal:5432/aiops_restored' \
  l1-agent audit verify --checkpoints checkpoints.jsonl
```

It should print `OK`. If a checkpoint is newer than the backup, it will say the chain no longer reaches that event: that is expected, and tells you what was lost between the backup and the failure.

The install test carries out this drill on every run: dump, restore beside the original, verify the chain in the copy, and check that row level security and the append-only audit log came back with the tables. It has been run against PostgreSQL in a test cluster, not against a managed service.

**Leaving with your data.** `l1-agent export create --out <dir>` writes SOPs, connections, policies, runs, actions and the audit log as JSON lines with a manifest. `l1-agent export verify --dir <dir>` checks an export, audit chain included, from the files alone. Users, sessions and tokens are not exported. There is no import.

## 8. When something is wrong

| What you see | Likely cause | What to do |
|---|---|---|
| The install job fails at `migrate` | The migration role cannot reach the database or does not own the schema | `kubectl logs job/<release>-install-<n> -c migrate` |
| The install job fails at `enrol-runner` | The application role is not a member of `l1_app`, or the name is enrolled with a different key | Section 2.2; or revoke the old runner first |
| API pods restart with "Refusing to start: the database role ..." | `database.urlKey` points at the migration role | Give it the application role |
| The runner logs "the control plane answered 401" | Its key is not enrolled, was revoked, or its clock is more than a minute out | `l1-agent runner list`; check time sync |
| The runner logs a certificate error | The CA in `runner.caSecret` did not sign the API's certificate, or the name does not match | Section 2.5 |
| Set-up shows the runner as not connected | It is not running, or cannot reach the control plane | `kubectl logs deploy/<release>-runner` |
| A step ends "No runner took the call in time" | No runner is connected | As above. The run has gone to a person; nothing was changed |
| A write ends "this runner has no list of its own" | `runner.targets` does not name the target | Section 5 |
| A write ends "goes no higher than T0" | `runner.maxTier` | Section 5 |
| A connection test ends "points outside the places this runner reads credentials from" | The reference is not under `/run/secrets/connectors/` or a listed prefix | Section 2.4 |
| The webhook answers 503 | No ServiceNow connection | Section 4, step 2 |

## 9. Limits you should know about

- One tenant per release. One kind of runner per tenant: every runner sees every call.
- The API's rate limit is counted per pod and per client address. Behind the dashboard or an ingress, every caller looks like one address. Put a limit in your ingress.
- Keys and bootstrap secrets are read at start-up. Replacing one means replacing the Secret and restarting the pods that use it. There is no overlap period for the dispatch or grant key: restart the workers, the API and the runner together.
- The worker and the runner have no health endpoint. A runner's health is whether the control plane has heard from it (`l1-agent runner list`, the Set-up page).
- Network policies are only enforced if your cluster's network plug-in enforces them.
- No SIEM export, no retention or deletion of old data, no encryption by the product (controls matrix: AU-04, AU-05, CR-04).

## 10. Trying it on one machine

`deploy/compose/docker-compose.yml` starts the same shape with Docker: PostgreSQL, the API, a worker, a runner holding mock connectors, and the dashboard on `http://127.0.0.1:8088`. It is a demonstration: the connectors are mocks, ServiceNow is a stand-in, nobody signs in, and its passwords are in the file.

```bash
docker compose -f deploy/compose/docker-compose.yml up --build
docker compose -f deploy/compose/docker-compose.yml run --rm incidents   # sends the sample incidents
```
