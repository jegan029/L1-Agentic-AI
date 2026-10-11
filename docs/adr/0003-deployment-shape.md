# ADR 0003: Deployment shape and tenancy

Status: accepted. D4 decided: lead with shape A, single tenant in the customer environment.

## Context

Buyers are regulated financial services firms. They will not allow inbound connections from a vendor cloud to Autosys, IBM MQ, a mainframe or internal file shares, and many will not allow ticket text or evidence to leave their environment. The current code is one process with one set of credentials.

## Options

### A. Single tenant, deployed in the customer environment

The whole product (API, UI, engine, workers, database, connectors) runs in the customer's Kubernetes cluster from a Helm chart.

| For | Against |
|---|---|
| No data leaves the customer. Simplest answer to outsourcing, residency and exit questions. | We do not operate it. Upgrades depend on the customer's change process; version spread across customers. |
| No vendor-side multi-tenant attack surface. | Support needs a diagnostics bundle design, since we cannot see the system. |
| We are a software supplier, not a service provider processing their data, which shortens procurement. | No shared telemetry for improving matching and SOP quality. |

### B. Shared control plane, customer-hosted runner

We host the API, UI, SOP lifecycle, policy and audit. The customer hosts a runner that holds connector credentials, makes outbound connections only, and executes connector calls.

| For | Against |
|---|---|
| We operate upgrades, one version, faster iteration. | Ticket text, evidence and the audit log are in our cloud. We become an ICT third-party service provider to a regulated firm, with the due diligence, contractual and resilience obligations that follow. |
| Lower effort for the customer to start. | A compromise of the control plane could issue actions to every customer's runner unless the runner enforces its own limits. |
| | Needs our own certifications before most buyers will sign. None exist today. |

## Decision

Build one codebase with two logical planes and a narrow protocol between them, and lead with shape A.

- **Control plane:** API, UI, SOP lifecycle, state machine, policy engine, approvals, audit log, ITSM and knowledge connectors where they are reachable.
- **Runner:** executes operations connector capabilities. Holds secret references for systems on the customer network. Pulls work over an outbound, mutually authenticated connection. Never accepts inbound connections.

In shape A both planes are deployed together and the protocol runs in-cluster. In shape B the control plane is ours and the runner is theirs. Moving from A to B is a deployment change, not a rewrite, provided the following are true from Phase 2:

1. Every row carries `tenant_id` and isolation is enforced at the data layer (ADR 0004), even when there is one tenant.
2. Connector calls go through the runner protocol even in shape A. No engine code calls an operations connector directly.
3. Secrets are resolved by the runner from the customer's secrets manager. The control plane stores references only.
4. The runner enforces a local ceiling that the control plane cannot raise: maximum risk tier, capability allow list, target allow lists, and a local kill switch. An action grant from the control plane is signed, bound to the action and parameter hash, short-lived and single use. The runner verifies it and refuses anything above its ceiling.
5. Evidence payloads can be kept in the customer's evidence store with only digests sent to the control plane, as a per-tenant setting.
6. Model provider configuration is per tenant, and "none" is valid.

## Consequences

- Extra design work now for the runner protocol and action grants, in exchange for not rebuilding later.
- Shape A needs strong day-two tooling: migrations that run unattended, a support bundle that redacts, health reporting that is honest.
- Shape B stays a later commercial choice. It should not be offered until the controls matrix has independent assurance behind it.

## Alternatives considered

- Multi-tenant SaaS with inbound access to customer systems through a VPN. Rejected: buyers will refuse it.
- Shape A with no plane split. Rejected: it makes shape B a rewrite and puts connector credentials in the same process as the web tier.

## As built in Phase 2

- Point 1 holds: every row carries `tenant_id` and PostgreSQL row level security enforces it, with one tenant or many.
- Points 2 to 5 are not built. Adapters still run inside the worker process; the runner split, action grants and the evidence store arrive with write actions and packaging (Phases 4 and 6).
- The ServiceNow connection, webhook secret, identity provider and adapters are configured per deployment, so one process serves one tenant. Per-tenant connections arrive with the connector registry in Phase 3.
