# Evaluation

The release focuses on reproducible local validation rather than inflated claims.

## Current validation surface

- source compiles,
- the CLI can execute an offline cycle,
- rollback utilities import and run,
- policy gates reject disallowed mutation paths,
- state persistence round-trips through disk,
- evidence can be regenerated from source.

## Recommended checks

```bash
pytest -q
python -m compileall -q src
PYTHONPATH=src python -m sif.cli --cycles 1 --json
python scripts/build_proof_pack.py
```

## What not to assume

Passing the included checks does not prove domain-level correctness for your use case. You still need domain-specific evaluation signals if you want to use the runtime in a serious environment.
