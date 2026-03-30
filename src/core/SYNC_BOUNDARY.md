# Sync boundary policy

The backend is **async-first**.

## Allowed sync entrypoint

- `cli.main()` is the only supported synchronous boundary and the only place that should call `asyncio.run`.

## Backend sync adapters

- Backend sync API does not exist.
- Supported backend adapters are async-only (`*_async`, `run_async`).
- `sif.core.cli_adapters` is not a supported backend surface and must not be reintroduced as a backend API.

## Enforced invariant

`tests/test_sync_boundary_invariants.py` statically checks backend modules (`src/core/spiral_engine.py`, `src/core/experiment_manager.py`, `src/core/evolution.py`, `src/core/versioning.py`, and `src/core/evaluator.py`) and fails when forbidden sync symbols are imported or reintroduced as module-level definitions.

## Strategy extension points are async-only

`SpiralEngine` strategy extension points must be implemented as async coroutines:

- `plan`
- `evaluate`
- `reflect`

Custom strategies must return awaitables. Synchronous strategy implementations are not supported and will fail at runtime when awaited.
