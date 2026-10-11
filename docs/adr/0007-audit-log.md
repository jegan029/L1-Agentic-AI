# ADR 0007: Tamper-evident audit log

Status: proposed, awaiting review. Engineering decision.

## Context

There is no audit log. Operational logs go to stdout with partial redaction. The brief requires an append-only, tamper-evident log, separate from operational logs, from which one incident can be reconstructed end to end.

## Decision

### Storage

An `audit_events` table in PostgreSQL, one hash chain per tenant plus one for platform-level events.

Each event has: sequence number (gapless per chain), tenant, time, actor (user, token, system component or runner, with identity provider subject), correlation id (run id), event type, a canonical JSON payload, the previous event's hash, and this event's hash computed over the canonical serialisation of all of the above.

The application role has INSERT and SELECT only. UPDATE, DELETE and TRUNCATE are revoked and also blocked by trigger. Appends take a per-chain advisory lock so sequence and previous-hash are consistent.

### Anchoring

A hash chain in a database proves internal consistency. It does not stop someone with database ownership from rewriting the whole chain. So at a configured interval the head of each chain is written as a signed checkpoint to a store the database administrator does not control: the customer's SIEM through the export, and an object store with write-once retention where the customer has one. Verification checks the chain and compares it against the checkpoints.

### Atomicity

Audit events are written in the same transaction as the state change they describe. A state transition without its audit event cannot commit. Events for external effects (a connector write) are written as intent before the call and result after, so a crash leaves evidence of an attempted write.

### What is recorded

Every item in section 7.2 of the brief. Payloads carry digests and redacted values, never secrets and never raw personal data beyond what the tenant's settings allow. For model calls: provider, model id, prompt template version, SHA-256 of the exact request and response bodies, token counts and the validated output. Full bodies go to the evidence store when the tenant enables that.

### Reconstruction

A `reconstruct` command and API take a run id and produce the full timeline from audit events alone, without reading operational tables. A test deletes the operational rows for a completed run in a scratch database and asserts the reconstruction is unchanged.

### Verification

`l1-agent audit verify --tenant T [--from N --to M]` recomputes the chain, checks sequence continuity, and compares against checkpoints. Exit status is non-zero on any mismatch. The same check runs on a schedule and raises an alert event.

### Export, retention, hold

- Export through an outbox to Splunk HEC, syslog (RFC 5424 over TLS) and OpenTelemetry logs, at least once, with the sequence number so the receiver can detect gaps.
- Retention per tenant. Expiry removes whole leading segments of a chain and writes a signed tombstone carrying the last removed hash, so the remaining chain still verifies.
- Legal hold flags a tenant, run or time range and blocks expiry. Setting and releasing a hold are audit events.
- Erasure requests under data protection law are handled by keeping personal data out of the chain in the first place (pseudonymous actor ids, redacted payloads) and holding the mapping in a table that can be erased. This needs review by a privacy professional.

### Evidence store

Larger artefacts (tool outputs, model bodies) go to an evidence store addressed by content hash, with the hash in the audit event. The interface supports a filesystem for demo mode and object storage with object lock for production. Encryption at rest uses a per-tenant key.

## Consequences

- Write throughput is serialised per tenant chain. Acceptable at this volume.
- The audit schema becomes a compatibility surface: event types are versioned and never repurposed.

## Alternatives considered

- Rely on the SIEM as the audit log. Rejected: the product must be able to prove its own history, and reconstruction must not depend on a third system.
- A ledger database or blockchain. Rejected: more infrastructure for the customer and no stronger than chain plus external checkpoints for this threat model.
