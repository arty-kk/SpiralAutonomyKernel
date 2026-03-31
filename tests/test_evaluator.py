# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

from sif.core import evaluator


def test_evaluator_uses_pytest_and_reports_success(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    async def fake_run_subprocess(cmd: list[str], *, timeout_s: float, cwd=None, env=None):
        commands.append(cmd)
        if cmd[2] == 'compileall':
            return 0, 'compile ok', '', False
        return 0, 'tests ok', '', False

    monkeypatch.setattr(evaluator, '_run_subprocess', fake_run_subprocess)
    monkeypatch.setattr(evaluator, 'run_benchmarks_async', lambda _workspace: asyncio.sleep(0, result={}))

    result = asyncio.run(evaluator.evaluate_async(tmp_path, benchmark_mode='never'))

    assert result['compile_success'] is True
    assert result['tests_success'] is True
    assert result['tests_status'] == 'passed'
    assert commands[1][0:4] == [commands[1][0], '-m', 'pytest', '-q']


def test_evaluator_marks_test_failure_when_pytest_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    async def fake_run_subprocess(cmd: list[str], *, timeout_s: float, cwd=None, env=None):
        if cmd[2] == 'compileall':
            return 0, 'compile ok', '', False
        return 1, '', 'tests failed', False

    monkeypatch.setattr(evaluator, '_run_subprocess', fake_run_subprocess)
    monkeypatch.setattr(evaluator, 'run_benchmarks_async', lambda _workspace: asyncio.sleep(0, result={}))

    result = asyncio.run(evaluator.evaluate_async(tmp_path, benchmark_mode='never'))

    assert result['compile_success'] is True
    assert result['tests_success'] is False
    assert result['tests_status'] == 'failed'
    assert result['tests_returncode'] == 1
