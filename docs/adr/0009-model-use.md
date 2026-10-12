# ADR 0009: Model use

Status: accepted (Phase 5)

## Context

After Phase 1 a model could do one thing: advise on which SOP candidate fits an incident. The call went straight from the engine to one OpenAI-compatible endpoint named in the environment. Ticket text was masked by pattern, and nothing limited what a tenant's data could be sent to, how much could be spent, or recorded what was sent.

Buyers in regulated firms ask four questions before they allow a model near production tickets: what can it make the system do, what does it see, who decided it may be used, and what does it cost when it misbehaves. This ADR answers each one by construction.

Decision D6 (see `docs/delivery-plan.md`) is fixed: the providers supported at launch are a customer-supplied OpenAI-compatible endpoint and Anthropic, and running with no model is a supported configuration.

## Decision

### The model advises on four things and decides none

| Task | The model is asked | What the engine does with the answer |
|---|---|---|
| `rank` | Which SOP candidate fits best | Takes the pick only if it is a candidate, clears the confidence threshold on the rules' own score, and, when it differs from the rules' best match, makes no change (has no `ACTION` step) |
| `extract` | Where in the ticket a named value is | Uses the value only if the SOP author marked the parameter for it, the SOP makes no change, the value stands in the ticket word for word as a whole word, and it passes the parameter's own validation |
| `interpret` | A hand-over summary of a run that did not resolve | Posts it to the ticket and shows it in the UI, labelled as model-drafted. It is written after the outcome is settled |
| `draft` | Structured steps from a prose article | Keeps them only if the result passes the SOP validator and changes nothing. It is a draft: one person submits it and a different person approves it before it can run |

Nothing else is sent to a model, and `ai/advisor.py` has no fifth method.

Each task has a prompt with a version and a closed answer schema. The provider is asked to call one named function. The answer is the arguments of that call or nothing: text is never read, and an answer that does not fit the schema is discarded. The schema validator (`ai/schema.py`) refuses to send a schema it could not fully enforce.

ADR 0001 said a model never supplies a raw argument. That still holds. In `extract` the model points at text that is already in the ticket; the engine then checks the text is there and fits the pattern the SOP author wrote. A ticket author could have put the same value where the author's own pattern would find it.

### One way out: the gateway

`ai/gateway.py → ModelGateway.ask` is the only caller of a provider. `tests/unit/ai/test_model_boundary.py` searches the source tree for any other, and for any place a provider client is built outside `ai/providers.py`. Each request goes through these steps in order:

1. **Policy.** The tenant's model policy is read from the database, every time. Is the model on, is this task enabled, is this provider on the tenant's list, with its stated region and retention.
2. **Redaction.** Everything to be sent is redacted, including the text the product itself wrote.
3. **Budgets.** Counted from the record of earlier calls.
4. **The provider**, with a time limit.
5. **Validation** of the answer against the task's schema.
6. **Record.** One row in `model_calls` and one audit event, in the same transaction.

`ask` returns the validated answer or nothing. It does not raise. A caller that gets nothing carries on as if there were no model.

### Providers

A provider implements one method: `complete(system, user, tool, max_output_tokens)`.

| Id | What it is | Verified against |
|---|---|---|
| `openai_compatible` | A chat completions endpoint with function calling, named by the customer | One hosted provider (`tests/live`, `python -m evals.run --live`) |
| `anthropic` | The Anthropic Messages API with one tool and a forced tool choice | Fixtures written by hand from the API documentation. Not run against the live API |
| `deterministic` | A stand-in that reaches no network. Demo mode, CI and the evaluation suite | Itself |

A deployment has one provider, set with `LLM_PROVIDER`, `LLM_ENDPOINT`, `LLM_MODEL` and `LLM_API_KEY` (or `LLM_API_KEY_REF`, read for each request so a rotation takes effect). Outside demo mode the endpoint must be `https`. The stand-in is refused outside demo mode.

### Tenant model policy

A tenant with no policy uses no model. Switching it on needs at least one provider and one task named.

```json
{
  "providers": ["openai_compatible"],
  "tasks": ["rank", "interpret"],
  "regions": ["eu"],
  "max_retention_days": 30,
  "budgets": {"incident_tokens": 20000, "daily_tokens": 1000000, "incident_cost_usd": 0.05, "daily_cost_usd": 20}
}
```

- `regions`: when set, the provider's stated region must be in the list. A provider with no stated region is refused.
- `max_retention_days`: when set, the provider's stated retention must be at most this. A provider with no stated retention is refused.
- Region and retention are **what the operator declares** in `LLM_REGION` and `LLM_RETENTION_DAYS`. The product enforces the tenant's rule against the declaration. It cannot check the declaration with the provider.
- An invalid policy is refused whole and changes nothing. A stored policy this version cannot read switches the model off.
- Changing the policy needs `policy.manage` (the tenant administrator) and is audited.

### Redaction

`ai/redaction.py` replaces secrets and personal data with placeholders such as `[[EMAIL_1]]` before anything is sent. The same value gets the same placeholder for the whole run. The map from placeholder to value stays in the process.

- **Secrets:** key and value pairs with a sensitive key name, authorisation headers, credentials in a URL, private key blocks, well-known token shapes, long hexadecimal strings, and long mixed-case tokens with high entropy.
- **Personal data:** email addresses, telephone numbers, card numbers (with a check digit test), IBANs (with a check digit test), US social security numbers, IPv4 addresses, and the name of the ticket's caller wherever it appears.
- **By field:** the caller field of a ticket is never sent.

Untrusted text is redacted, then cut to a length limit, then placed between `<<<` and `>>>`. The prompt says such text is data. That delimiting is a mitigation. The control is that the answer can only be one of four schema-checked shapes, and what the engine does with each is fixed.

When a hand-over summary is written back to the ticket the text came from, personal data placeholders are replaced with the original values. Secret placeholders are not.

This is pattern matching. A person's name in running text, a postal address, or a secret with no recognisable shape is not found. `evals/redaction.json` lists what is found and, under `not_found`, examples of what is not; the test asserts both lists.

### Budgets

Per incident and per tenant per day (UTC), in tokens and optionally in cost. Before a request is sent, its size plus the most output it may use is added to what the record already holds. If that passes the budget the request is not sent, a `model_calls` row with outcome `over_budget` and a `model.budget_exceeded` audit event are written, and the advisor takes no further part in that run, including after a resume.

- A provider that reports no token counts is charged an estimate. A provider error is charged for what was sent.
- Cost uses the prices the operator declares (`LLM_INPUT_PRICE_PER_MTOK`, `LLM_OUTPUT_PRICE_PER_MTOK`).
- The daily budget is checked without a lock. Several workers at once can pass it by the size of the requests in flight.

### The record

`model_calls` holds, for each request: task, provider, model id, prompt version, SHA-256 of what was sent, SHA-256 of the validated answer, token counts, cost, and outcome (`ok`, `invalid`, `error`, `over_budget`). Neither the prompt nor the answer is stored. The table is append only (a grant and, on PostgreSQL, a trigger). The audit event `model.called` carries the same digests, with the run id as correlation id.

### Prompts

`ai/prompts.py` holds each prompt with a version. A test pins the digest of each version, so the text cannot change without the version changing, and another test requires every version to appear in `docs/model-risk/change-log.md` and `docs/model-risk/model-inventory.md`.

### Evaluation

`evals/` holds three corpora and the checks that run them. `python -m evals.run` and `tests/evals/` call the same functions.

- **Golden incidents:** eleven incidents with the outcome and capability calls expected when no model is involved. With the model off, failing, returning garbage, hostile, or fully obeying injected text, every one must be unchanged.
- **Injection:** hostile text in tickets, in tool output and in articles, including directives that the "obedient" stand-in model follows to the letter. Measured inside each connector's `invoke`: every call must be the capability and the parameters of the step it was made for, worked out again from the published SOP and the ticket, and there must be no write.
- **Redaction:** seeded items placed in a ticket, in tool output and in an article; all four tasks run through both provider clients against a local HTTP server; no item may appear in any request body.

`python -m evals.run --live` runs the golden and injection corpora against the configured provider. It costs money and is run on purpose.

## Engineering choices made here

- **The model package keeps its name** (`ai/`). The target architecture called it `advisor/`; renaming would have moved every existing test for no gain.
- **Model use is off for a new tenant**, as actions are in shadow for a new tenant. This changes an existing deployment: `LLM_ENABLED=true` alone no longer sends anything until the tenant's policy allows it.
- **A model cannot steer a run into an SOP that makes a change.** Without this rule a model's ranking could choose a restart procedure over a read-only one when a ticket fits both.
- **A parameter located by a model never reaches a write.** The validator rejects an SOP that has both an `ACTION` step and a model-assisted parameter, and the executor refuses independently.
- **A located parameter is journalled** on the run, so a resumed run uses the first answer and does not ask again.
- **No model request is retried.**
- **Answers are not stored**, only their digests. ADR 0007 planned to store the validated answer; it can hold ticket text, so it waits for the evidence store and a tenant setting.
- **The older `AIAnalyzer` class remains** for existing unit tests and as the one-shot demo's fallback. It talks to a client directly, with the Phase 1 masking. A test asserts nothing the service runs can build one.

## Not done

- One provider per deployment. A tenant's list decides whether that provider may be used; it does not choose between several.
- Names and addresses in running text are not redacted.
- No store of full prompts and answers for later review.
- No automatic comparison of model versions over time; the evaluation suite is run by hand against a real provider.
- No per-tenant price list or currency.
- The policy rules are edited as JSON through the API or `l1-agent model set`. The UI shows them and switches the model on and off.

## Consequences

- Two tables, `model_policies` and `model_calls`, and a column `runs.advised_parameters` (migration `0004`).
- A hostile model can still do three things, all bounded: choose between read-only SOPs that both fit a ticket, point at a different piece of the ticket's own text for a parameter the author opened to it, and write misleading text in a note that is labelled as unverified. The threat model lists these as residual risks.
- A model that reasons before answering spends its output allowance on the reasoning. The per-task limits were raised after the first live run cut answers short.
