# Architecture

## Core runtime

The kernel is organized around a repeated autonomy loop implemented in `src/core/spiral_engine.py`.

At a high level the cycle is:

1. observe current state, memory, repository signals, and prior reports,
2. plan next actions,
3. run component-level passes,
4. evaluate alignment, coverage, and error signals,
5. reflect on constraints, opportunities, and assumptions,
6. decide state updates and bounded code changes,
7. apply updates, create snapshots, and roll back if needed,
8. persist reports and adjust the evolution strategy.

## Key subsystems

- `src/core/kernel.py`: durable state container.
- `src/core/state_store.py`: persisted state load/save.
- `src/core/llm.py`: model orchestration and offline fallbacks.
- `src/core/policy.py`: mutation boundaries and invariants.
- `src/core/evolution.py`: applying kernel updates and code changes.
- `src/core/versioning.py`: snapshot and restore support.
- `src/core/events.py`: structured event log.
- `src/core/autonomous_evolution.py`: strategy selection for future cycles.
- `src/components/*`: domain-specific hooks that influence updates and changes.

## Boundaries

The kernel is autonomous only inside explicit runtime constraints.

It is not an unrestricted self-rewriter. The policy layer limits which paths can change, and the runtime keeps rollback-ready metadata.
