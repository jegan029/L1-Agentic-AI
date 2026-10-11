# Model change log

One entry for each change to a prompt, an answer schema, a provider client, the redaction rules, or the limits a model works within. Newest first.

A test (`tests/unit/ai/test_schema_and_prompts.py`) fails if a prompt version in the code is not named on this page, and pins the digest of each version so its text cannot change silently.

The customer's own change of model id or provider is not recorded here. It is in the tenant's audit log: every `model.called` event carries the provider and the model id.

## 2026-10-11: Phase 5

### Prompt versions

| Version | SHA-256 | Change |
|---|---|---|
| `rank/2` | `230bcd59…ad070` | Replaces the unversioned Phase 1 prompt. Same task. The text now says the answer is advice, that data sections are never instructions, and that placeholders stand for removed values. The model is no longer asked to judge confidence below 0.4; the engine's own score decides |
| `extract/1` | `29998a79…33387` | New task |
| `interpret/1` | `896550f5…484b7` | New task |
| `draft/1` | `02e21c4f…e3b1c` | New task |

### Answer schemas

- `rank`: `sop_id` (one of the candidates, or `NONE`), `rationale` (at most 600 characters), optional `confidence` (0 to 1, not used by the engine).
- `extract`: one optional string for each requested parameter, each at most the parameter's own maximum length.
- `interpret`: `summary` (at most 800 characters), `likely_cause` (one of seven fixed values), `next_checks` (at most three, 200 characters each).
- `draft`: steps of six types (no `ACTION`), a capability chosen from the read capabilities listed in the request, parameters, keywords.

### Providers

- `openai_compatible`: now behind the gateway. The request names the one function, as before.
- `anthropic`: new. Written from the API documentation; not run against the live API.
- `deterministic`: replaces the mock client in the service path.

### Limits

- Output allowance per task: `rank` 1200, `extract` 800, `interpret` 1500, `draft` 4000 tokens. First set to 400, 300, 600 and 3000; raised after a live run in which a model that reasons before answering had 7 of 16 answers cut short.
- Untrusted text: 4,000 characters per section; 16,000 for an article.
- Default budgets: 20,000 tokens per incident, 1,000,000 per tenant per day.

### Redaction

Replaces the Phase 1 masking for everything the gateway sends. Adds placeholders, telephone numbers, card numbers, IBANs, social security numbers, IPv4 addresses, the caller's name, long random tokens, and more secret shapes.

### Evaluation

- Stand-in models: all ten checks pass.
- Real model, `python -m evals.run --live`, one hosted model through the OpenAI-compatible client: every property held. Details in [validation.md](validation.md).
