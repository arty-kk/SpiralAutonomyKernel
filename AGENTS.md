# Contributor and Agent Guidelines

This repository is for a bounded autonomous runtime. Changes should make the system easier to trust, reproduce, and operate.

## Product boundary

The repository is about **persistent self-evolution for LLM-based systems inside an explicit policy boundary**.

Do not reposition it as:

- a general AGI platform,
- a cyber-operations framework,
- a vague digital lifeform project,
- a generic coding assistant.

The core contract is narrower:

- maintain durable state,
- run repeated improvement cycles,
- propose bounded self-modifications,
- evaluate outcomes,
- adapt strategy,
- preserve rollback and observability.

## Repository priorities

Prefer changes that improve one of these properties:

1. correctness of the autonomy loop,
2. clarity of runtime boundaries,
3. reliability of rollback and recovery,
4. observability of decisions and outcomes,
5. reproducibility of validation and evidence.

## Changes that are in scope

Good changes usually improve at least one of:

- state handling,
- evaluation signals,
- reflection quality,
- policy enforcement,
- versioning and rollback,
- event logging,
- LLM orchestration contracts,
- CLI operability,
- tests and evidence generation,
- documentation of guarantees and limits.

## Changes that are out of scope

Avoid adding:

- inflated capability claims,
- marketing language that outruns implementation,
- unrelated product surfaces,
- opaque network-heavy behavior without tests,
- self-modification outside declared policy,
- hidden side effects that make evaluation harder.

## Prompt design rules

All prompts and model-facing instructions in this repository must:

- be in English,
- ask for structured outputs,
- describe the system as a bounded autonomy controller,
- avoid mystical, anthropomorphic, or dominance-oriented language,
- keep claims consistent with the actual runtime.

Use language that emphasizes a persistent runtime, bounded self-modification, measurable improvement, rollback readiness, policy-gated autonomy, and structured evaluation. Avoid inflated, mystical, anthropomorphic, or dominance-oriented framing.

## Engineering expectations

When changing runtime logic:

- preserve offline behavior when no API key is present,
- prefer deterministic fallbacks,
- keep file mutations explicit and reviewable,
- maintain rollback readiness,
- avoid widening allowed mutation paths without a strong reason,
- add or update tests for behavior changes.

## Release bar

A release candidate should meet all of the following:

- source and prompts are in English,
- public positioning matches the actual implementation,
- the package imports cleanly,
- the CLI completes a smoke cycle,
- tests pass,
- the evidence pack regenerates from source,
- no obvious junk files or abandoned drafts remain in the root.
