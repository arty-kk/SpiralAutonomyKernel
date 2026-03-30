#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

PYTHONPATH=src python -m sif.cli --cycles 1 --json
