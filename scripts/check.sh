#!/usr/bin/env bash
set -euo pipefail

pytest -q
python -m compileall -q src
PYTHONPATH=src python -m sif.cli --cycles 1 --json >/dev/null
python scripts/build_proof_pack.py
