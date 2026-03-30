# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_cli_smoke_json(tmp_path: Path) -> None:
    env = dict(os.environ)
    env['PYTHONPATH'] = 'src'
    state_path = tmp_path / 'state.json'
    proc = subprocess.run(
        [sys.executable, '-m', 'sif.cli', '--cycles', '1', '--json', '--state-path', str(state_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    assert 'observations' in payload
    assert 'plan' in payload
    assert 'evaluation' in payload
    assert state_path.exists()
