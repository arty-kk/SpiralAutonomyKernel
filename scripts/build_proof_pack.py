# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / 'docs' / 'evidence'
RUNTIME_DIR = ROOT / '.sif'


def _run(command: list[str], retries: int = 0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env['PYTHONPATH'] = 'src'
    attempts = retries + 1
    last_result: subprocess.CompletedProcess[str] | None = None
    for _ in range(attempts):
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        if result.returncode == 0:
            return result
        last_result = result

    assert last_result is not None
    command_str = " ".join(command)
    raise RuntimeError(
        f"Command failed after {attempts} attempt(s): {command_str}\n"
        f"stdout:\n{last_result.stdout}\n"
        f"stderr:\n{last_result.stderr}"
    )


def main() -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    if RUNTIME_DIR.exists():
        shutil.rmtree(RUNTIME_DIR)

    try:
        compile_result = _run([sys.executable, '-m', 'compileall', '-q', 'src'])
        test_result = _run([sys.executable, '-m', 'pytest', '-q'], retries=1)
        smoke_result = _run([
            sys.executable,
            '-m',
            'sif.cli',
            '--cycles',
            '1',
            '--json',
            '--state-path',
            str(ROOT / '.tmp-proof' / 'state.json'),
        ])

        offline_cycle = json.loads(smoke_result.stdout)
        (EVIDENCE_DIR / 'offline-cycle.json').write_text(
            json.dumps(offline_cycle, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

        validation_md = f'''# Validation

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
{test_result.stdout.strip()}
```
'''
        (EVIDENCE_DIR / 'validation.md').write_text(validation_md, encoding='utf-8')

        runtime_summary_md = f'''# Runtime Summary

## Observations

- goals: {offline_cycle['observations'].get('goals')}
- constraints: {offline_cycle['observations'].get('constraints')}
- internal_constraints: {offline_cycle['observations'].get('internal_constraints')}
- external_constraints: {offline_cycle['observations'].get('external_constraints')}

## Evaluation

- alignment: {offline_cycle['evaluation'].get('alignment')}
- coverage: {offline_cycle['evaluation'].get('coverage')}
- errors: {offline_cycle['evaluation'].get('errors')}

## Reflection

{offline_cycle['reflection'].get('summary', '')}
'''
        (EVIDENCE_DIR / 'runtime-summary.md').write_text(runtime_summary_md, encoding='utf-8')
    finally:
        if RUNTIME_DIR.exists():
            shutil.rmtree(RUNTIME_DIR)
        tmp_proof = ROOT / '.tmp-proof'
        if tmp_proof.exists():
            shutil.rmtree(tmp_proof)


if __name__ == '__main__':
    main()
