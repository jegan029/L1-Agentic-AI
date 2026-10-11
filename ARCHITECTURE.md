# L1 Engineer Agent: architecture as built

This describes the code as it stands after Phase 1 of the [delivery plan](docs/delivery-plan.md). It is a single-process service with in-memory state. Where the product is going is in [docs/target-architecture.md](docs/target-architecture.md); the reasons for the main choices are in [docs/adr/](docs/adr/).

Only the ServiceNow client and the model client have been run against real systems (a developer instance and one hosted model provider). Every tool adapter has only been run against its mock.

## 1. Shape

```
ServiceNow ──signed webhook──▶┐
ServiceNow ◀──poll (New, one assignment group)──┤
                               ▼
                     IncidentProcessor
                       │  duplicate? ─▶ no run
                       │  SOPMatcher  ─▶ no SOP / low confidence ─▶ escalated
                       │  (optional model advice on ranking)
                       ▼
                     SOPExecutor
                       validate SOP ─▶ bind parameters ─▶ run steps through the state machine
                       │                                   │
                       │                                   └─▶ adapters (read only):
                       │                                       Splunk, IR360/MQ, Autosys, Windows share,
                       │                                       Dynatrace, browser check, mainframe TN3270
                       ▼
                     ExecutionSummary (one outcome)
                       ├─▶ ServiceNow: work notes; Resolved, or reassigned to an L2 group
                       ├─▶ history store (memory + JSONL) ─▶ dashboard API
                       └─▶ event bus ─▶ server-sent events ─▶ React dashboard
```

One aiohttp process holds everything: the listener, the poller, the engine and the stores. A semaphore limits how many incidents run at once (`AGENT_MAX_CONCURRENT_INCIDENTS`).

## 2. One engine

There is one execution path and it is deterministic ([ADR 0001](docs/adr/0001-one-execution-engine.md)). A model never chooses a tool, a step or an argument.

If a model is configured (`LLM_ENABLED=true`), it is asked one question: which of the candidate SOPs fits this ticket, or none. Its answer is used only if the SOP it names already clears the confidence threshold on the matcher's own score. The engine gives the same guarantees with the model switched off.

## 3. Outcomes

Every run ends in exactly one of four outcomes ([ADR 0002](docs/adr/0002-fail-closed-outcomes.md)).

| Outcome | Reached when |
|---|---|
| `resolved` | A VERIFY step passed and the SOP then reached a RESOLVE step, and ServiceNow accepted the Resolved update. |
| `escalated` | The SOP reached an ESCALATE step, a check failed with no other route, no SOP matched, or the match was below the threshold. |
| `needs_human` | The engine could not continue safely: invalid SOP, unbound parameter, missing adapter, open circuit breaker, internal error, or ServiceNow refused the Resolved update. |
| `aborted` | Reserved for an operator stop. Nothing triggers it yet. |

Three independent layers stop an unverified resolution:

1. **SOP validator** (`sop/validator.py`). An SOP is rejected at load unless every route to a RESOLVE step passes the success edge of a VERIFY step, with only NOTE steps in between.
2. **Run state machine** (`engine/state_machine.py`). The transition table is data. `RESOLVED` can be entered only from `VERIFIED`; an unknown state and event pair goes to `NEEDS_HUMAN`.
3. **Summary type** (`models/evidence.py`). `ExecutionSummary` raises if `RESOLVED` is set without the id of the step that verified it.

`tests/unit/engine/test_no_unverified_resolve.py` searches the table and every sample SOP for a route around these, and runs random SOP graphs against adapters that pass, fail and raise.

## 4. SOP matching

`engine/sop_matcher.py` scores each published, valid SOP against the ticket on four signals.

| Signal | Weight | How it is scored |
|---|---|---|
| Keywords | 0.50 | Distinct SOP keywords found as whole words in the ticket text, plus the share of the SOP title's words in the short description. Three hits is full score. |
| CI | 0.20 | The ticket's CI matches one of the SOP's services. |
| Category | 0.15 | The ticket's category or subcategory is one the SOP lists. |
| Assignment group | 0.15 | The ticket's group is one the SOP lists. |

A signal is scored only when both sides carry it. If the ticket has no CI, or the SOP lists no services, that signal is left out and the other weights are rescaled. A value that is present and different counts against the match. Past results for the SOP then move the score by up to 10 percent either way.

The run proceeds only if the best score reaches `AGENT_SOP_CONFIDENCE_THRESHOLD` (default 0.6). Matching fields alone, with no keyword evidence, score 0.5 and do not clear it.

## 5. SOP execution

An SOP is a JSON document: typed parameters and a graph of steps.

- **Parameters** are bound before any step runs, from a ticket field, a regular expression over ticket text, or a constant. Each is validated against its type and pattern. An unbound or invalid parameter ends the run as `needs_human`.
- **Check steps** call one adapter each. Step parameters are validated against a closed schema for that adapter (`adapters/schemas.py`) before the call; unknown keys are refused.
- **DECISION and VERIFY steps** evaluate an expression over earlier step results. The expression language is a small parser in `engine/expressions.py`; nothing is passed to `eval`.
- **A step result** is `success`, `fail` (the check ran and the answer was bad; follow `on_failure`) or `error` (the engine could not run it; end as `needs_human`).

Adapter calls are retried with exponential backoff and sit behind a per-adapter circuit breaker.

## 6. Escalation

Anything that is not `resolved` goes through `escalation/escalator.py`: the incident's CI is matched against the patterns in the routing file (`L2_ROUTING_PATH`), the ticket is reassigned to that L2 group, the evidence is written as work notes, and an email is sent if SMTP is configured. The packaged routing file is an example with placeholder addresses.

## 7. Security as built

The target controls are in [docs/threat-model.md](docs/threat-model.md) and [docs/controls-matrix.md](docs/controls-matrix.md). What exists now is an interim layer.

- **Access.** Every route is denied unless it carries a configured bearer token. The exceptions are `/health`, which returns no detail, and the webhook, which needs an HMAC signature with a timestamp and rejects replays. Outside demo mode the service will not start without both secrets.
- **Network.** The listener binds to loopback by default. Cross-origin access is limited to listed origins. The service does not serve TLS itself.
- **Adapters.** All are read only. Each validates its inputs against an allow list, and an empty allow list denies. TLS verification cannot be turned off outside demo mode.
- **Model boundary.** Ticket text is redacted, length-limited and delimited before it is sent. Model output is never logged and is only ever read as an SOP choice.
- **Logs and secrets.** Logs are JSON with pattern-based redaction of message text and sensitive keys. Secrets fetched from Vault or AWS are passed to the settings object and are not written to the process environment.

## 8. What this design does not have yet

| Gap | Consequence today | Phase |
|---|---|---|
| No database or durable queue | A restart loses in-flight incidents and the duplicate record. | 2 |
| One tenant | One set of credentials and one routing file per process. | 2 |
| No sign-in or roles | Shared bearer tokens; no per-user attribution. | 2 |
| No audit log | Only operational logs and ServiceNow work notes. | 2 |
| No connector framework, no OAuth to ServiceNow | Basic authentication; adapters are hard-wired. | 3 |
| No SOP review workflow | SOPs are files; the demo editor has no approval step. | 3 |
| No write actions | The agent diagnoses and verifies; it does not remediate. | 4 |
| No model evaluation suite | Redaction and injection resistance are unit tested, not measured on a corpus. | 5 |
