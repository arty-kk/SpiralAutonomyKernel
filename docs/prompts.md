# Prompt Policy

The model-facing instructions in this repository are written in English and keep the system inside a grounded frame.

## Prompt design goals

Prompts should describe the runtime as:

- a bounded autonomy controller,
- a persistent improvement loop,
- a structured JSON producer,
- a system that respects explicit constraints and rollback logic.

## Prompt design rules

Prompts should avoid:

- anthropomorphic identity inflation,
- claims of unrestricted autonomy,
- inflated or dominance-oriented language,
- vague metaphysical framing.

## Expected outputs

Prompts should request structured JSON for:

- plans,
- evaluations,
- reflections,
- code-change proposals.

## Supported runtime tasks

`LLMOrchestrator` currently supports exactly these task keys:

- `plan`
- `evaluate`
- `reflect`
- `code_changes`

Each task must map to an explicit runtime path and fallback payload.
