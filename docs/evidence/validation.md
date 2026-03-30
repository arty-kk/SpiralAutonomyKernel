# Validation

## Commands

```bash
python -m compileall -q src
pytest -q
PYTHONPATH=src python -m sif.cli --cycles 1 --json --state-path .tmp-proof/state.json
```

## Results

- compile: success
- tests: success
- smoke cycle: success

## Pytest output

```text
..........                                                               [100%]
10 passed in 2.61s
```
