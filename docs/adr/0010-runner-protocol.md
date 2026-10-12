# ADR 0010: The runner and its protocol

Status: accepted, built in Phase 6. Completes points 2 to 4 of [ADR 0003](0003-deployment-shape.md).

## Context

ADR 0003 split the product into a control plane and a runner so that a hosted control plane could follow the single-tenant shape without a rewrite. Until Phase 6 the split was on paper: connectors ran inside the worker, and a grant for a write was minted and checked by the same process with a key that process held ([ADR 0008](0008-actions-policy-and-approvals.md)).

Three things had to be true of the split for it to be worth having:

1. Credentials for the systems on the customer's network are read where the runner is and nowhere else.
2. The customer's network accepts no connection from the control plane.
3. A control plane that has been taken over cannot make the runner do more than the runner's operator allowed, and cannot make it write at all without the policy engine's key.

## Decision

### Shape

The runner is the same code started with `l1-agent runner run`. It has no database and listens on no port. It makes one kind of connection: outbound HTTPS to the control plane.

With `RUNNER_MODE=remote` the control plane makes no operations connector call itself. `ConnectorHub.for_tenant` returns a `RemoteConnectorSet`, which has the shape of a `ConnectorSet`, so the engine and the action pipeline are unchanged. Its `invoke` signs the call, puts it in the `runner_calls` table and waits for the answer. The control plane still builds each connector object from the tenant's connection, to check parameters against the manifest's schema and the tenant's allow lists before anything is queued, but never invokes it.

`RUNNER_MODE=embedded` remains: calls are made in the worker, as before. It is the default of the code, so a deployment configured before Phase 6 behaves as it did. The Helm chart's default is `remote`.

The ITSM connection and the SOP sources (ServiceNow knowledge base, Git) are still called from the control plane. Whether they should move depends on what is reachable from where in a hosted shape, which is still open (target architecture, open point 1).

### Two routes, both called by the runner

| Route | What it does |
|---|---|
| `POST /runner/v1/claim` | Asks for calls. Held open for up to 20 seconds while there are none. The runner says what its own ceiling is, for display |
| `POST /runner/v1/calls/{id}/result` | Returns one call's result. Accepted once, and only from the runner that holds the call |

Both are declared with the `runner()` access rule: they accept a runner's signature and nothing else. A browser session or an API token is refused.

### Three key pairs

| Key | Private half held by | Public half held by | Signs |
|---|---|---|---|
| Dispatch | API and workers | Runner | Every call handed to a runner |
| Grant | Workers only | Runner | Every grant for a write |
| Runner | The runner | Control plane (enrolled by a tenant administrator) | Every request the runner makes |

All are Ed25519. Each signature is made over a purpose label followed by the content, so a signature made for a call cannot be presented as a grant or as a request.

The grant key is separate from the dispatch key so that the API tier, which faces the network and must be able to queue a connection test, never holds the key that authorises a write. A runner with no grant public key refuses every write.

The runner pins the two public keys of its control plane in its own configuration. They are not learned over the connection.

### What the runner checks before it makes a call

In this order, and before any credential is read:

1. The control plane's signature on the envelope; that the envelope is for this runner's tenant, has not expired, and has not been seen before.
2. Its own ceiling: the capability is one it carries out; the capability's declared tier is no higher than its maximum (T0 unless raised); for a write, its own kill switch is off and every target the call names is on a list the runner itself holds; every credential reference points into a place its operator listed.
3. What `Connector.invoke` checks for any call: the closed parameter schema, the allow lists in the connection, and for a write a grant, signed by the policy engine, for exactly this call, unused and unexpired.

The ceiling is read from environment variables and files where the runner runs. No route exists by which the control plane could change it.

A connection's settings (the target's address, its allow lists) and its credential references travel in the envelope. An operator who does not want that for a connector lists the connection in the runner's own file (`RUNNER_CONNECTIONS_FILE`), and what the control plane sends for that connector is then ignored.

### When an answer does not come

| Situation | What the caller gets | What happens to the run |
|---|---|---|
| No runner took the call in time | The call is withdrawn so it can never be made; `RunnerUnavailable` | The step is an error; the run goes to a person |
| A runner took a read and did not answer | `RunnerUnavailable` | The same. A read is not retried through the queue |
| A runner took a write and did not answer | `OutcomeUnknown` | The write is not sent again. The pipeline reads the target, as it does after a crash at that point: if the target shows the result the action was for, the run goes on; if not, the action is recorded as "not known" and the run goes to a person |

The kill switches are read from the database again when a runner asks for work. A write that was queued before a switch was engaged is answered "not authorised" there and is not handed out. Which runner was handed a write is an audit event in the run's trail.

### Identity

A runner is enrolled by giving its public key a name (`POST /api/runners`, or `l1-agent runner enrol`). It then identifies itself either by the identity shown at enrolment, which names the tenant, or by its key, which a control plane that serves one tenant resolves. The second form lets an install job enrol a key before the runner first starts, with no second step. One key is one runner.

Revoking a runner takes effect on its next request: the row is read every time, with no cache.

## Consequences

- Operators have three key pairs to make, store and eventually replace. Keys are read at start-up; replacing one means replacing the file and restarting.
- Every operations connector call costs a database round trip and a poll. On a quiet system a call adds a few hundred milliseconds.
- A result passes through the database on its way back, and is removed when it has been read. A row abandoned by a crash is removed after thirty minutes.
- The runner's own target list is one more place where a job name has to be listed before it can be restarted. That is the point of it.
- A runner sees every call for its tenant. There is no routing of some connectors to one runner and others to another.

## What this does not do

- **No mutual TLS in the product.** The runner authenticates the control plane by its TLS certificate and itself by a signature on each request. Where an ingress demands a client certificate the runner can present one (`RUNNER_CLIENT_CERT_FILE`); that path has not been tested.
- **A control plane that has been taken over can still ask for reads.** It holds the dispatch key. Within the runner's ceiling it can ask for any read, and it supplies the connection's address, so it could point a connector at a host of its choosing and receive that connector's credential there. The defences are the ceiling's capability list, the reference prefixes, connections held by the runner itself, and outbound network rules around the runner, which are the operator's to set. The chart does not restrict outbound traffic.
- **Replay after a restart.** Call ids and grant nonces that have been used are remembered in memory. The queue hands a call to one runner once, so replaying one needs the dispatch key or a break of TLS; even so, a restarted runner would accept a recorded call again until it expires.
- **Evidence is not kept at the customer's site only.** Results travel to the control plane and into the run record. Point 5 of ADR 0003 (digests only) is not built.
- **No key rotation procedure**, no certificate-based enrolment, no hardware-backed keys.

## Alternatives considered

- **Mutual TLS with tenant-bound certificates, as the threat model first proposed.** The server we use does not expose the client certificate to the application, so binding a certificate to a tenant would have to be done by an ingress or a mesh we do not control. Signatures at the message level work the same wherever TLS ends.
- **A long-lived connection (WebSocket or gRPC stream).** Lower latency. More to operate, and harder to get through the proxies buyers have. Polling over HTTPS needs nothing but outbound 443.
- **One key for calls and grants.** Simpler, and it would put the power to authorise a write in the web tier.
- **The runner holds all connection settings and the control plane holds none.** The strongest position against a hostile control plane, and it would take connector set-up out of the product. Offered per connector instead (`RUNNER_CONNECTIONS_FILE`).

## As built

`src/l1_agent/runner/` (`keys.py`, `protocol.py`, `store.py`, `dispatch.py`, `ceiling.py`, `agent.py`), `src/l1_agent/connectors/sdk/grant.py` (`GrantCheck`), `src/l1_agent/api/access.py` (`verify_runner`), migration `0005`. Tests: `tests/runner/`, and `tests/e2e/test_done_means.py` for the whole path. The structural test that pins where a grant verifier may be attached (`tests/unit/actions/test_write_needs_a_decision.py`) now names two places: the engine, and the runner.

Verified with the control plane and the runner in one process (tests), on one machine (the compose demo, with PostgreSQL) and in one local Kubernetes cluster (kind, over TLS with a private CA). Never across a real network boundary, through a proxy, or against real target systems.
