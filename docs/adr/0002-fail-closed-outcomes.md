# ADR 0002: Fail-closed outcomes

Status: proposed, awaiting review

## Context

Both executors initialise the outcome to RESOLVED and change it only on some failures. Verified results: an SOP with no steps resolves; a failed step routed to a NOTE resolves; an unrecognised decision rule takes the success branch; the memory scenario resolves with memory at 92.5 percent. "Resolved" currently means "the checks ran".

## Decision

### Terminal states

| State | Meaning | Effect in the ITSM |
|---|---|---|
| RESOLVED | A VERIFY step ran after the last action and its assertion passed, showing the symptom is absent | Resolve the incident with the verification evidence |
| ESCALATED | The SOP or the engine routed the incident to a named team | Reassign, add the evidence packet |
| NEEDS_HUMAN | The engine could not continue safely: validation failure, unbound parameter, unknown rule, connector unavailable, budget exhausted, internal error, timeout | Leave open, flag for an operator, add what is known |
| ABORTED | Stopped by an operator or the kill switch | Leave open, note who stopped it |

There is no FAILED terminal state. A failure is a reason attached to NEEDS_HUMAN or ESCALATED, because every failure needs a person and the state should say so.

A run has no outcome field until it reaches a terminal state. The type is "absent or one of four", so a default cannot exist. A duplicate delivery does not create a run and so has no outcome.

### Rules enforced in three places

1. **Static, at SOP publication.** The validator analyses the step graph and rejects the SOP unless: it has at least one step; every reference resolves; every unset failure edge is treated as ESCALATE; every route to a RESOLVE step passes the success edge of a VERIFY step with no ACTION step between that VERIFY and the RESOLVE; the graph is acyclic or every cycle has a declared bound.
2. **Dynamic, in the transition function.** The only transition into RESOLVED is from VERIFYING, and it requires a persisted verification record for this run whose assertion passed and which is later than the last action record. Any exception, any unknown step type, any unknown rule, any missing connector, and loop exit by any other route transitions to NEEDS_HUMAN.
3. **Structural, at the data layer.** A database check constraint requires `verification_id` to be non-null when the run's outcome is RESOLVED.

### What VERIFY is

A VERIFY step calls a read capability and applies an assertion written by the SOP author in the typed expression language, for example `autosys.job_status in ["SU", "RU"]` or `dynatrace.mem_usage_pct < 85`. It has two exits: pass and fail. It can be retried with a declared wait and count, because many symptoms take time to clear. An inconclusive result (connector error, missing field) is a fail.

A check-only SOP can resolve if its VERIFY passes, which covers incidents where the symptom cleared on its own. A check-only SOP whose VERIFY fails ends ESCALATED with a diagnosis. The memory scenario therefore ends ESCALATED.

## The test the brief asks for

1. **Engine table search.** The transition table is data. A test builds the graph of engine states and enumerates every path from RECEIVED to RESOLVED, asserting that each passes the VERIFYING state on its pass edge.
2. **SOP graph search.** For every sample SOP and every published fixture, enumerate all paths through the step graph and assert the same property.
3. **Property-based.** Generate random SOP graphs with Hypothesis. For graphs the validator accepts, execute with adversarial step results (every combination of pass, fail, error, timeout up to a bound) and assert the outcome is never RESOLVED without a passed verification record. For graphs the validator rejects, assert the engine refuses to start.
4. **Mutation guard.** A test deletes the verification requirement from a copy of the transition table and asserts that test 1 then fails, so the search cannot silently become vacuous.

## Consequences

- The measured auto-resolution rate will drop sharply at first, because existing "resolutions" were not resolutions. This should be said to stakeholders before Phase 1 lands. The honest metric becomes "resolved with verification" alongside "diagnosed and routed".
- Existing tests that assert RESOLVED after a single read step encode the defect and will change. Each is listed in the delivery plan.
- SOP authors must write a verification for anything they want auto-resolved. That is the right burden.

## Alternatives considered

- Keep RESOLVED as default and add more failure checks. Rejected: this is the current design and each new step type reopens the hole.
- Let the model judge whether the symptom cleared. Rejected as the deciding control. The advisor may label evidence, but the assertion that gates RESOLVED is deterministic.
