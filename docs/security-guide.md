# Security guide

For a security reviewer at a firm that is considering, or running, L1 and L2 AI-Ops. It says what the product is trusted to do, where the boundaries are, what protects each one, and what does not exist. The detail behind each statement is in the [threat model](threat-model.md), the [controls matrix](controls-matrix.md) and the ADRs in [adr/](adr/).

**Read this first.** The product holds no certification and has had no penetration test and no independent review. Every statement here is engineering's own, backed by automated tests against mocks, hand-written fixtures and local test environments. It has run against one ServiceNow developer instance and one hosted model, and against no real Autosys, Splunk, Dynatrace, IBM MQ, mainframe or identity provider. Where something is not built, this guide says so.

## 1. What it is allowed to do

It reads incidents from ServiceNow, matches each to a published procedure (an SOP), and carries out that procedure's steps. Nearly every step is a read: a Splunk search, a job status, a queue depth. One kind of change exists: an Autosys force start of a job. Nothing else can be changed, because nothing else is declared as a capability, and a connector cannot be asked for something it has not declared.

Three properties hold by construction, each with a test that searches for a way round it:

| Property | Why it holds | Evidence |
|---|---|---|
| A model never chooses what runs and never supplies an argument | The steps and their parameters come from the SOP version. A model can advise on four narrow things, and the engine re-checks each answer | `tests/unit/ai/test_model_boundary.py`, the injection corpus in `evals/` |
| An incident is never marked resolved without a verification that passed | The outcome is reached through a table of transitions that has no other route to it | `tests/unit/engine/test_no_unverified_resolve.py` |
| No change is made without a recorded policy decision | A write needs a grant that only the policy engine can sign, and it signs only after the database has recorded that the action may execute | `tests/unit/actions/test_write_needs_a_decision.py`, `tests/runner/test_grant_verification.py` |

## 2. The parts and what each one holds

```
 browser ──TLS──> dashboard ──> API ──────────────┐
 ServiceNow ──signed webhook──> API               │
                                 │                 ▼
                               workers ──────> PostgreSQL (row level security)
                                 │
                 signed calls, signed grants       ▲ outbound only, TLS,
                                 ▼                 │ every request signed
                               runner ─────────────┘
                                 │
                 Splunk, Autosys, Dynatrace, IBM MQ, file shares
```

| Part | Reachable from | Holds | Does not hold |
|---|---|---|---|
| Dashboard | Users' browsers | Nothing | Any credential |
| API | The dashboard, ServiceNow, the runner | Database access as the application role; the webhook secret; the key that signs calls to the runner | The key that signs grants; any connector credential (with a runner) |
| Worker | Nothing | Database access as the application role; both signing keys | Any connector credential (with a runner) |
| Runner | Nothing | Its own key; the credentials of the systems it calls, read at the moment of each call | Any database access; any private key of the control plane |
| Install job | Nothing | Database access as the migration role, while it runs | |

The ServiceNow credential and the credentials of SOP sources are used by the control plane, not the runner.

## 3. The boundaries and what protects each

### 3.1 People to the product

Sign-in is through your identity provider (OpenID Connect or SAML). There are no local passwords except one break-glass account, which is off unless you set its hash, signs in as an administrator for an hour, and is audited every time. Sessions are server-side; the cookie is `HttpOnly`, `Secure` and `SameSite=Lax`; a state-changing request also needs a token the page reads from the API. Every route declares who may call it, and the service refuses to start if one does not.

Seven roles, fixed in code. The separations that matter: the person who writes an SOP cannot approve it; the person who writes an SOP cannot approve a tier 2 action from it; the administrator who sets how far the product may go cannot approve what it does; only an auditor reads the audit log. Each is enforced in the service and again by a database constraint. A machine token can read and cannot administer or approve.

Two accounts held by one person defeat the separations. The product compares identities, not people.

### 3.2 ServiceNow to the product

A webhook carries an HMAC of its body and a timestamp, and each signature is accepted once. Ticket text is data: it is matched against SOP keywords, and values are taken from it only by a pattern the SOP author wrote and then checked against a type. A ticket cannot name a tool, a host or a command.

### 3.3 Tenants from each other

Every table but two carries a tenant, and PostgreSQL row level security filters every query by the tenant set on the transaction. The application connects as a role that cannot bypass it, and refuses to start as one that can. A test walks every table and every API route with another tenant's data present. One release serves one tenant today, so this boundary is there for the hosted shape and as a second line.

### 3.4 Control plane to runner

This is the boundary that matters most if the control plane is ever operated by someone else, and it is designed as if it were ([ADR 0010](adr/0010-runner-protocol.md)).

- The runner listens on nothing. It connects out, over TLS, and asks for work.
- Every request it makes is signed with a key whose public half an administrator enrolled. Revoking that enrolment stops it at its next request.
- Every call it is handed is signed by the control plane, names one tenant, expires, and is made once.
- A write also needs a grant signed by the policy engine with a different key, which the API tier never holds. The runner can check a grant and cannot make one.
- The runner has limits of its own, set where it runs: the highest tier it will go to (reads only, unless raised), which capabilities it will carry out, which targets it will change, and where it will look for a credential. Nothing sent to it can raise them.
- Credentials are read by the runner, from your secrets manager or a mounted file, at the moment of the call. They are never sent to the control plane.

**What a control plane that had been taken over could still do.** It could ask the runner for reads within the runner's limits. Because a connection's address is sent with each call, it could point a read at a host of its choosing and so receive the credential the runner presents there. It could not make a write without the grant key, which is in the workers. Defences you control: list the capabilities the runner will carry out, hold sensitive connections in the runner's own file so that the control plane's version is ignored, and restrict where the runner may connect.

**Not built:** mutual TLS inside the product. The runner authenticates the server by its certificate and itself by signature.

### 3.5 The product to a model

Off unless a tenant's model policy turns it on. When on, everything sent is passed through redaction first, which is pattern matching: it removes recognised shapes of secret and personal data and will miss others. A model's answer is accepted only if it fits a closed schema, is never logged or stored, and is re-checked by the engine before it has any effect. A run ends the same way with the model off, failing, or hostile; the evaluation suite checks that on every build. See [ADR 0009](adr/0009-model-use.md) and [model-risk/](model-risk/).

### 3.6 The product to the audit log

Every state change writes an audit event in the same transaction. Events are chained by hash per tenant; the application role can add and cannot change or delete; `audit verify` recomputes the chain. Someone who owns the database can rewrite the whole chain consistently. That is detectable only against a signed checkpoint you have stored elsewhere, which is a step you have to take (RUNBOOK 4).

## 4. Keys and secrets

| Secret | Form | Where it lives | Replacing it |
|---|---|---|---|
| Connector credentials | A reference (`vault:`, `aws:`, `file:`) | Your secrets manager or a mounted Secret; read per call | Takes effect on the next call, no restart |
| Dispatch key, grant key, runner key | Ed25519, PEM file | Kubernetes Secrets, mounted read-only | Replace the Secret and restart; no overlap period |
| Webhook secret, OIDC client secret, database URLs | Value | Kubernetes Secret, as environment variables | Replace and restart |
| Session and API tokens | Random; only a SHA-256 hash is stored | Database | Revocable; tokens expire |

The product stores no connector credential, in the database or anywhere else. The Vault and AWS backends have not been run against real services.

## 5. Supply chain

| What | State |
|---|---|
| Python dependencies | Every package, direct and transitive, pinned to a version with hashes; the image installs with `--require-hashes` |
| Base images | Pinned by digest |
| Dashboard dependencies | Installed from the lock file, with integrity hashes |
| Static analysis, secret scan | Run in CI and block |
| Dependency audit, image scan | Defined in CI as blocking, and not yet run there. Run once by hand: the audits found nothing; the image scan's findings (packaging tools bundled with the base image, an old nginx base) were fixed, after which it found nothing high or critical with a fix available |
| Bill of materials | Defined in CI. **Never produced yet** |
| Signed images | A release workflow signs each image digest. **No release has been made**, so no signed image exists |
| Third-party code at run time | None loaded. The dashboard asks no other origin for anything unless a deployment turns a font on |

## 6. A hardening checklist

Each of these is yours to do; the product cannot do it for you.

- [ ] Deploy by image digest. When signed releases exist, verify the signature, and have an admission controller refuse unsigned images.
- [ ] Give the grant key to workers only. Start without it, and without `runner.grantPublicKey`, until you intend the product to make a change.
- [ ] Leave the runner at T0 until then. When you raise it, list every target it may change in `runner.targets`.
- [ ] Add egress rules for the runner: the control plane, your secrets manager, the systems it is for. Nothing else.
- [ ] Check that your cluster enforces network policies at all.
- [ ] Put the dashboard and the API behind your ingress with TLS, and put a rate limit there.
- [ ] Turn on encryption at rest and encrypted backups for the database.
- [ ] Store audit checkpoints outside the database, on a schedule.
- [ ] Map identity provider groups to roles so that no one person holds both sides of a separation.
- [ ] Use a ServiceNow integration user with the least it needs (the connector's notes in Set-up say what).
- [ ] Leave the tenant in shadow until you have read what it would have done.
- [ ] Set the `BRANDING_DENYLIST` secret if you build from source and have names that must never appear.

## 7. What does not exist

Said once here so that nobody has to infer it.

- No certification, attestation, penetration test or independent review.
- No export to a SIEM. The audit log is read through the API or exported to files.
- No retention, deletion or legal hold. The product deletes nothing.
- No encryption of data at rest by the product.
- No mutual TLS inside the product; no key rotation procedure; no hardware-backed keys.
- No restriction of outbound traffic by the chart.
- No protection against someone with code execution in a worker forging a grant: the grant key is in that process. The runner's own limits still apply.
- No protection of the audit log against the database owner without external checkpoints.
- No sandbox around the browser connector, which is a preview and whose browser is not in the image.
- No evidence from any real target system other than one ServiceNow developer instance.

## 8. Reporting a problem

There is no public process yet. Until there is, report a suspected vulnerability privately to whoever supplied the software to you, and do not open a public issue for it.
