# Contributing

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Validation

```bash
pytest -q
python -m compileall -q src
python scripts/build_proof_pack.py
```

## Pull request expectations

A good change should:

- fit the repository boundary,
- keep prompts and docs in English,
- add tests for behavior changes,
- keep claims narrower than or equal to the implementation,
- preserve rollback and observability contracts.
