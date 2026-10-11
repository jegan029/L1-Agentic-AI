# Sub-processors

Status: after Phase 5.

## Single-tenant deployment

The customer runs the product in its own infrastructure. The vendor does not receive or process the customer's data, so the vendor has no sub-processors for it.

The customer chooses every system the product talks to: its IT service management system, its operations tools, its identity provider, its secrets manager and, if it enables one, a model provider.

### Model provider

A model provider receives data only when all of these are true:

1. The deployment is configured with a provider (`LLM_ENABLED`, `LLM_PROVIDER`, `LLM_ENDPOINT`, `LLM_MODEL`).
2. A tenant administrator has switched model use on for the tenant and named that provider and at least one task.
3. The provider's stated region and retention satisfy the tenant's rules, where it has set any.

What it receives is described in [the model inventory](model-risk/model-inventory.md), section 4. The relationship with the provider is the customer's: the customer holds the contract and decides the provider's role.

The product supports two kinds of provider and names no particular vendor as a requirement:

| Kind | What it is |
|---|---|
| `openai_compatible` | Any endpoint the customer names that offers chat completions with function calling. This includes a model the customer hosts itself |
| `anthropic` | The Anthropic Messages API |

## Hosted deployment

Not offered yet. This list will name each sub-processor before a hosted service is made available.
