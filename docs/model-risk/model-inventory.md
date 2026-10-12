# Model inventory and intended use

Status: after Phase 5. The design is [ADR 0009](../adr/0009-model-use.md). Changes are in [change-log.md](change-log.md) and the evidence in [validation.md](validation.md).

This document is written for a model risk function. It describes how the product uses a language model. It is an engineering description, not the outcome of an independent validation: the customer's own model risk process decides whether the use is acceptable, and this document gives it the facts.

`tests/unit/ai/test_schema_and_prompts.py` fails if a prompt version or a provider kind in the code is missing from this page.

## 1. What is in the inventory

The product does not ship a model. It calls one that the deployment is configured with. The inventory therefore has two parts: what the product fixes, and what the customer chooses.

### Fixed by the product

| Item | Value | Where |
|---|---|---|
| Tasks | `rank`, `extract`, `interpret`, `draft` | `src/l1_agent/ai/advisor.py` |
| Prompt versions | `rank/2`, `extract/1`, `interpret/1`, `draft/1` | `src/l1_agent/ai/prompts.py` |
| Answer format | One function call per request, checked against a closed schema | `src/l1_agent/ai/schema.py` |
| Provider kinds | `openai_compatible`, `anthropic`, `deterministic` | `src/l1_agent/ai/providers.py` |

`deterministic` is a stand-in that calls nothing. It is used in demo mode, in CI and in the evaluation suite, and is refused outside demo mode.

### Chosen by the customer

| Item | Set with | Recorded |
|---|---|---|
| Provider kind and endpoint | `LLM_PROVIDER`, `LLM_ENDPOINT` | Provider kind in every `model.called` audit event |
| Model id | `LLM_MODEL` | In every `model.called` audit event and `model_calls` row |
| Stated region and retention | `LLM_REGION`, `LLM_RETENTION_DAYS` | Shown on the Actions page and by `GET /api/model` |
| Whether a tenant uses it, and for which tasks | The tenant's model policy | `model.policy_changed` audit events |

The model id in use at any time, and every change of it, can be read from the audit log. The product keeps no separate register of customer models.

## 2. Intended use

To help an operations team triage incidents with published procedures, in four narrow ways:

| Task | Intended use | Decision it informs | Who or what decides |
|---|---|---|---|
| `rank` | Suggest which candidate SOP fits a ticket when more than one clears the confidence threshold | Which read-only procedure runs | The engine, by fixed rules. A pick is ignored unless the rules alone would also allow it |
| `extract` | Point to a value in the ticket that the SOP author's pattern did not find | Whether a read-only procedure can run for this ticket | The engine. The value must be in the ticket word for word and fit the author's pattern |
| `interpret` | Summarise a run that did not resolve, for the engineer who takes over | None | The engineer |
| `draft` | Turn a prose article into a first draft of structured steps | None until a person submits it and a different person approves it | Two people |

## 3. Uses that are out of scope and prevented

- Choosing a tool or a capability, or supplying an argument that was not checked against the ticket and the SOP.
- Causing, approving or influencing any change to a target system.
- Deciding the outcome of a run, or that an incident is resolved.
- Any use for a tenant whose model policy does not allow it.

Each is prevented in code and measured by the evaluation suite. None relies on the model following its instructions.

## 4. Inputs

Ticket fields (short description, description, category, subcategory, configuration item, assignment group), the summary of each check's result, and article text. All of it is redacted before it is sent. The caller field is never sent. Section 6 states what redaction does not catch.

## 5. Outputs and how they are used

| Task | Output | Stored | Shown to people |
|---|---|---|---|
| `rank` | A candidate id and a short rationale | The rationale, in the run record and the `run.sop_matched` audit event, prefixed "Model-drafted rationale" | Yes, in the work note |
| `extract` | Parameter values | The accepted values on the run; their digests in the audit log | Yes, in a work note marked "Model-assisted" |
| `interpret` | Summary, a cause from a fixed list, up to three suggestions | In the run record | Yes, marked "Model-drafted summary", not verified, not used for any decision |
| `draft` | Steps, parameters, keywords | As a draft SOP version with origin `model_drafted` | Yes, with a warning on the review page |

## 6. Limitations

- **Redaction is pattern matching.** It does not find a person's name in running text (other than the ticket's caller), a postal address, or a secret with no recognisable shape. `evals/redaction.json` lists examples under `not_found`.
- **Region and retention are declarations.** The product enforces a tenant's rule against what the operator states about the provider. It cannot verify the statement.
- **A model can be wrong or manipulated within its bounds.** It can pick the less suitable of two read-only procedures that both fit, point at a different piece of the ticket's own text, or write a misleading summary. The summary is labelled; the other two are limited to read-only procedures.
- **Budgets are estimates before the call.** Token counts come from the provider afterwards. The daily budget can be passed by the size of the requests in flight when several workers run at once.
- **The Anthropic client has not run against the live API.** It is tested against fixtures written from the documentation.
- **One provider per deployment.**
- **Quality is not measured.** The evaluation suite measures what the system does with any answer, not how good the answers are. Whether `rank` picks well or `interpret` summarises well for a given customer's tickets has not been assessed.

## 7. Human oversight

- A tenant administrator switches model use on, per task, and can switch it off at any time. The next request is refused; nothing is cached.
- Every request is in the audit log with digests, token counts and outcome.
- Text written by a model is labelled wherever it is shown.
- A model-drafted SOP needs two people before it can run.

## 8. Monitoring

- `GET /api/model` and `l1-agent model usage`: today's requests, tokens and cost, by outcome.
- `GET /api/model/calls`: recent requests with digests.
- Metrics `model.calls` (by task and outcome) and `model.refused` (by reason).
- A rising share of `invalid` or `error` outcomes is the signal that a provider or a model has changed.

## 9. When to re-run the evaluation

Run `python -m evals.run --live` and record the result in the change log when any of these changes: the model id, the provider, a prompt version, an answer schema, or the redaction rules.
