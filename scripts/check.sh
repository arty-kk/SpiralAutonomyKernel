#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

python scripts/check_license_headers.py
pytest -q
python -m compileall -q src
PYTHONPATH=src python -m sif.cli --cycles 1 --json >/dev/null
python scripts/build_proof_pack.py
