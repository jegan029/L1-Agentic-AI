# ADR 0001: One execution engine

Status: proposed, awaiting review

## Context

There are three execution paths: a rule-based step walker (`engine/`), an LLM tool-calling loop (`ai/`) and a CrewAI four-agent pipeline (`crew/`). Each decides the outcome in a different way: a default value, a tool call chosen by the model, and a regex over free text. Each would need its own audit, policy and approval integration. Two of them let a model choose which tool to call and with what arguments.

## Decision

A deterministic, persisted SOP state machine is the only thing that executes. The SOP version, not a model, determines which capability runs at each step and with which parameter template.

The model is an advisor at four bounded points. Each is a pure function with a JSON Schema for its output, a deterministic fallback, and no access to connectors.

| Advisor task | Input | Output (validated) | Fallback when the model is off, over budget or invalid |
|---|---|---|---|
| Rank SOP candidates | Redacted ticket fields, candidate list from the keyword matcher | Ordered candidate ids with confidence, drawn only from the supplied list | Keyword matcher score |
| Extract parameters | Redacted ticket text, the SOP's parameter schema | Values for declared parameters only | Regex and field extraction declared in the SOP; if still unbound, NEEDS_HUMAN |
| Interpret evidence | Redacted tool output, a fixed set of labels declared by the step | One label from the set | The step's deterministic assertion; if none, NEEDS_HUMAN |
| Draft notes | Run record | Text for a work note, marked as model drafted | Template rendering |

Extracted parameter values are validated against the parameter's schema and allow list by the engine before use. An interpretation label can select between branches the SOP author wrote; it cannot create a branch, choose a capability or supply an argument.

## What to keep

From `ai/`:

- `llm_client.py`: keep the HTTP client and response parsing as the first provider implementation behind the provider interface. Remove `chat_with_tools` (the tool loop) and the logging of model output.
- `mock_llm.py`: keep, reshaped as the deterministic provider for demo mode and the evaluation suite.
- `analyzer.py`: keep `select_sop` as the basis for the rank task, with schema validation and candidate-list restriction. Drop `analyze_incident` free-text analysis from the execution path.
- `tool_definitions.py`: the parameter schemas move into connector manifests. The model-facing tool list is deleted.
- `ai_executor.py`: delete.

From `crew/`: nothing in the execution path. The role prompts are a useful reference for advisor prompt wording. Remove the `crewai` and `crewai-tools` dependencies; they bring a large transitive tree (vector stores, embedding clients, CLI tooling) that would dominate the SBOM and the vulnerability backlog for no retained function.

From `engine/`: `sop_matcher.py` stays as the candidate generator. `executor.py` evolves into the state machine; its retry and circuit breaker wiring moves into the connector SDK. `resolution_memory.py` is replaced by a query over the run table. `incident_processor.py` is rewritten as a thin job handler; this is cheaper than evolving it because most of its 485 lines are two near-identical copies of the escalation flow for the AI and rule-based paths.

## Consequences

Positive

- One place to enforce policy, approvals, the kill switch and audit.
- Prompt injection cannot cause a tool call, because no model output is ever interpreted as one.
- The product works with the model disabled, which some regulated buyers will require at first.
- Runs are replayable and testable without a model.

Negative

- The product cannot handle an incident that has no approved SOP. It hands it to a human. This is the intended position for this market, but it is a narrower promise than "agentic".
- More authoring effort per SOP, since branches and parameters must be explicit. The draft-from-prose assistance in the SOP lifecycle offsets this.
- "Multi-agent" disappears as a selling point.

## Alternatives considered

- Keep the LLM tool loop with a code-level tool filter per step. Rejected: the model would still choose arguments and call order, the audit story stays "the model decided", and the injection corpus target of zero unauthorised calls becomes probabilistic.
- Keep CrewAI for triage only. Rejected: it adds a framework and dependency tree to do what one schema-validated call does.
