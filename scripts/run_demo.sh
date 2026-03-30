#!/usr/bin/env bash
set -euo pipefail

PYTHONPATH=src python -m sif.cli --cycles 1 --json
