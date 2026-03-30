# Spiral Autonomy Kernel

Spiral Autonomy Kernel is an open-source runtime for **persistent, policy-bounded self-evolution in LLM-based systems**.

It is not positioned as a general AGI stack, a cyber-operator, or a coding assistant. The product goal is narrower and more useful:

> run repeated self-improvement cycles, keep durable memory, propose bounded self-modifications, measure the outcome, and roll back when gains do not hold.

## What problem it solves

Most LLM agents are still session-bound and operator-dependent.

They can answer, draft, and react, but they usually do **not**:

- preserve a durable improvement loop across runs,
- maintain a structured memory of goals, constraints, and prior outcomes,
- propose controlled self-modifications inside a declared boundary,
- adapt their own evolution strategy based on measured signals,
- recover cleanly when a change degrades behavior.

Spiral Autonomy Kernel exists for teams and researchers that want to work on this narrower but important problem:

**how to turn an LLM-driven agent into a long-running system that can improve itself in bounded, inspectable, and recoverable cycles.**

## What the kernel does

The runtime executes a recurring improvement loop:

- observe current goals, constraints, memory, repository state, and prior metrics,
- plan the next actions,
- run component-level reasoning passes,
- evaluate alignment, coverage, and error signals,
- reflect on constraints, opportunities, and assumptions,
- propose kernel updates and bounded code changes,
- apply changes with validation and rollback support,
- revise the evolution strategy for the next cycle.

The loop is persistent rather than session-only. The system records state, reports, events, snapshots, and prior decisions between runs.

## Why test it

The main reason to evaluate Spiral Autonomy Kernel is not novelty language. It is operational leverage for autonomous-agent work:

- fewer manual resets between runs,
- durable learning and state accumulation,
- explicit self-modification boundaries,
- reproducible improvement cycles,
- measurable evolution strategy changes,
- rollback when self-modification harms performance.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
pytest -q
```

Run one offline cycle with structured JSON output:

```bash
PYTHONPATH=src python -m sif.cli --cycles 1 --json
```

Run continuously in unattended mode:

```bash
PYTHONPATH=src python -m sif.cli \
  --continuous \
  --continue-on-error \
  --max-consecutive-errors 3 \
  --restart-on-fatal \
  --max-restarts 3 \
  --sleep-seconds 1
```

Roll back to the latest saved snapshot:

```bash
PYTHONPATH=src python -m sif.cli --rollback latest
```

Regenerate the evidence pack:

```bash
python scripts/build_proof_pack.py
```

## What to test first

### You are researching persistent self-improvement
Start with:

- `python -m sif.cli --cycles 1 --json`
- `python scripts/build_proof_pack.py`
- `docs/architecture.md`
- `docs/evaluation.md`

### You care about safe self-modification boundaries
Start with:

- `src/core/policy.py`
- `tests/test_policy.py`
- `tests/test_code_mutation.py`
- `docs/guarantees.md`

### You care about recovery and unattended execution
Start with:

- `tests/test_versioning.py`
- `tests/test_cli.py`
- `docs/deployment.md`
- `docs/evidence/offline-cycle.json`

## Evidence

The repository ships checked-in evidence under `docs/evidence/` and a script that regenerates it from source.

Start with:

- `docs/evidence/validation.md`
- `docs/evidence/offline-cycle.json`
- `docs/evidence/runtime-summary.md`

## What this repository proves

- a persistent autonomy loop can run end to end,
- state, events, and reports survive between cycles,
- bounded self-modification can be proposed and validated,
- the runtime can maintain rollback-ready snapshots,
- offline fallback behavior works when no model API key is configured,
- an evolution strategy can be revised based on observed signals.

## What it does not claim

- general artificial superintelligence,
- unrestricted autonomy over arbitrary systems,
- automatic correctness without evaluation signals,
- production deployment safety without operator review,
- universal improvement on every task or domain,
- unconstrained self-rewrite of the whole repository.

Those boundaries are deliberate. They make the project more credible and easier to evaluate.

## Read next

- `docs/problem-solution-profit.md`
- `docs/architecture.md`
- `docs/evaluation.md`
- `docs/guarantees.md`
- `docs/deployment.md`
- `docs/prompts.md`

## License

Licensed under the Apache License, Version 2.0.

Copyright © 2026 Сацук Артём Венедиктович (Satsuk Artem).
