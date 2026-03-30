# Deployment

## Local execution

```bash
PYTHONPATH=src python -m sif.cli --cycles 1 --json
```

## Unattended execution

```bash
PYTHONPATH=src python -m sif.cli \
  --continuous \
  --continue-on-error \
  --max-consecutive-errors 3 \
  --restart-on-fatal \
  --max-restarts 3 \
  --sleep-seconds 1
```

## Operator guidance

Before using the runtime on a valuable repository:

- inspect `src/core/policy.py`,
- confirm which paths can be mutated,
- test snapshot creation and restore,
- run the kernel offline first,
- enable model-backed behavior only after prompt review.
