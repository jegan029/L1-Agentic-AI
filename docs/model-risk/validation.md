# Validation evidence for model use

Status: after Phase 5. What was tested, how, and what was not. See [model-inventory.md](model-inventory.md) for intended use and limitations.

This is evidence from the product's own automated tests. It is not an independent validation.

## 1. What is claimed

Whatever a model returns, including nothing, garbage, or exactly what an attacker's injected text asks for:

1. No capability call is made other than the one a published SOP step declares, with the parameters that step renders from the ticket.
2. No change is made to a target system that would not have been made with no model.
3. No run ends resolved without a verification step that passed.
4. No seeded secret or personal data item of a recognised shape reaches a provider.
5. Past a budget, the advisor takes no further part in the run and the run ends as it would have with no model.

## 2. How it is measured

**At the connector boundary.** The evaluation harness (`evals/harness.py`) builds a tenant the way the service does and records every call inside each connector's `invoke`. Expected calls are worked out again from the published SOP and the ticket, without reference to anything a model returned (`evals/checks.py → calls_are_what_the_sop_declares`).

**At the HTTP boundary.** For redaction, both provider clients are pointed at a local HTTP server that keeps the raw request bodies. The seeded values are searched for in those bytes.

**Against stand-in models that misbehave** (`evals/providers.py`):

| Stand-in | Behaviour |
|---|---|
| Obedient | Follows any directive it finds in the prompt it is sent. This assumes a prompt injection has fully succeeded |
| Hostile | Always gives the most damaging answer that still fits the schema |
| Garbage | A differently malformed answer on each request |
| Failing | Errors, and requests that never return |
| Helpful | A correct answer for each task |

## 3. Corpora

| Corpus | File | Size |
|---|---|---|
| Golden incidents | `evals/golden.json` | 11 incidents: 3 resolved, 6 escalated, 2 needing a person; 3 include a write |
| Injection | `evals/injection.json` | 7 tickets, 2 tool-output cases, 2 articles |
| Redaction | `evals/redaction.json` | 24 seeded items of 8 kinds; 3 documented gaps |

The corpora are small and written by the same people who wrote the code. They show the controls work on the cases thought of. They are not a substitute for testing with a customer's own tickets and articles.

## 4. Results

### Stand-in models (every CI run)

`python -m evals.run` and `tests/evals/`. All ten checks pass:

- Golden incidents with the model off, the deterministic stand-in, garbage, failing, hostile and obedient: outcome, SOP, verification step, capability calls and number of writes identical to the expectation in every case.
- Golden incidents with a helpful model: one incident changes, as designed (the assisted procedure runs because the model located the queue manager); the others are identical.
- Injection, 9 incident cases against 4 misbehaving models: no write, no call outside the step's declared capability and parameters.
- Injection, 2 articles against 5 models: every result is a draft; no draft has an `ACTION` step, a write capability or a change template; a model's steps are kept only when they pass the validator.
- Redaction: none of the 24 seeded items in any of 8 requests (4 tasks, 2 clients).

### Mutation check (Phase 5, once)

25 guards were removed one at a time. A test or an evaluation check failed for each. The list covers: the rank rules, the located-value rules, redaction of each kind tested, schema validation, policy, region, retention, both token budgets, the stop after a budget, the audit event, the label on model text, and the resume journal.

### A real model (2026-10-11, once)

`python -m evals.run --live` against one hosted model through the OpenAI-compatible client (model id `nvidia/nemotron-3-ultra-550b-a55b`).

- Golden: 11 incidents, 16 requests, all answers valid, about 27,000 tokens. Every property held. Outcomes matched the expectations; the assisted incident resolved with the located value `QMPROD01`.
- Injection: 9 incident cases. Every property held: no write, no call outside the declared capability.
- A first run the same day had 7 of 16 answers cut short by output limits that were too low for a model that reasons before it answers. The limits were raised (see the change log). The properties held in that run too, because a cut-off answer is discarded.

This is one run against one model. A real model's answers vary between runs.

## 5. Not tested

- The Anthropic client against the live API.
- Article drafting and redaction against a real model. The live run covers `rank`, `extract` and `interpret`.
- Answer quality for any task.
- Languages other than English.
- Long tickets near the length limits, and very large candidate lists.
- Behaviour under provider rate limiting.
- Several workers reaching a daily budget at the same moment.
