# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

from sif.core.candidates import Candidate
from sif.core.evolution import CodeChange
from sif.core.experiment_manager import ExperimentManager
import sif.core.experiment_manager as experiment_manager_module


class _InMemoryCacheStore:
    _data: dict[str, object] = {}

    def __init__(self, _cache_path) -> None:
        self._namespace = str(_cache_path)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def get(self, key: str):
        return self._data.get(f'{self._namespace}:{key}')

    async def put_many(self, payload: dict[str, object]) -> None:
        for key, value in payload.items():
            self._data[f'{self._namespace}:{key}'] = value


def _patch_cache_store(monkeypatch) -> None:
    _InMemoryCacheStore._data.clear()
    monkeypatch.setattr(experiment_manager_module, 'AsyncCacheStore', _InMemoryCacheStore)


async def _passing_evaluator(_workspace: Path) -> dict[str, object]:
    return {
        'compile_success': True,
        'tests_success': True,
        'tests_skipped': False,
        'duration_sec': 0.01,
    }


def _prepare_repo(repo_root: Path) -> None:
    (repo_root / 'src' / 'components').mkdir(parents=True)
    (repo_root / 'tests').mkdir(parents=True)
    (repo_root / 'src' / 'components' / '__init__.py').write_text('', encoding='utf-8')
    (repo_root / 'tests' / 'test_smoke.py').write_text('def test_smoke():\n    assert True\n', encoding='utf-8')


def test_experiment_manager_accepts_candidate_with_valid_metrics(monkeypatch, tmp_path: Path) -> None:
    _patch_cache_store(monkeypatch)
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    _prepare_repo(repo_root)

    manager = ExperimentManager(
        repo_root=repo_root,
        evaluator=_passing_evaluator,
        cache_path=repo_root / '.sif' / 'cache' / 'evals.json',
    )
    candidate = Candidate(
        id='accepted',
        source='test',
        code_changes=[CodeChange(path='src/components/generated_candidate.py', content='x = 1\n')],
    )

    best_candidate, results = asyncio.run(manager.run_async([candidate]))

    assert best_candidate is not None
    assert best_candidate.id == 'accepted'
    assert results['accepted']['accepted'] is True
    assert results['accepted']['reason'] == 'accepted'


def test_experiment_manager_rejects_malformed_metrics_payload(monkeypatch, tmp_path: Path) -> None:
    _patch_cache_store(monkeypatch)
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    _prepare_repo(repo_root)

    async def malformed_evaluator(_workspace: Path) -> dict[str, object]:
        return {'compile_success': True, 'duration_sec': 0.02}

    manager = ExperimentManager(
        repo_root=repo_root,
        evaluator=malformed_evaluator,
        cache_path=repo_root / '.sif' / 'cache' / 'evals.json',
    )
    candidate = Candidate(
        id='malformed',
        source='test',
        code_changes=[CodeChange(path='src/components/generated_candidate.py', content='x = 2\n')],
    )

    best_candidate, results = asyncio.run(manager.run_async([candidate]))

    assert best_candidate is None
    assert results['malformed']['accepted'] is False
    assert results['malformed']['reason'] == 'malformed_metrics'
    assert 'missing_required_metrics' in results['malformed']['error']


def test_experiment_manager_reports_blocked_timeout_and_cached_outcomes(monkeypatch, tmp_path: Path) -> None:
    _patch_cache_store(monkeypatch)
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    _prepare_repo(repo_root)
    cache_path = repo_root / '.sif' / 'cache' / 'evals.json'

    async def dynamic_evaluator(workspace: Path) -> dict[str, object]:
        marker = workspace / 'src' / 'components' / 'mode.txt'
        mode = marker.read_text(encoding='utf-8').strip() if marker.exists() else 'accepted'
        if mode == 'timeout':
            await asyncio.sleep(0.05)
        return {
            'compile_success': True,
            'tests_success': mode != 'reject',
            'tests_skipped': False,
            'duration_sec': 0.01,
        }

    manager = ExperimentManager(
        repo_root=repo_root,
        evaluator=dynamic_evaluator,
        cache_path=cache_path,
        timeout_per_candidate=0.01,
    )

    accepted = Candidate(
        id='accepted',
        source='test',
        code_changes=[CodeChange(path='src/components/mode.txt', content='accepted\n')],
    )
    blocked = Candidate(
        id='blocked',
        source='test',
        code_changes=[CodeChange(path='src/core/blocked.py', content='x = 1\n')],
    )
    timeout = Candidate(
        id='timeout',
        source='test',
        code_changes=[CodeChange(path='src/components/mode.txt', content='timeout\n')],
    )

    best_candidate, results = asyncio.run(manager.run_async([accepted, blocked, timeout]))

    assert best_candidate is not None
    assert best_candidate.id == 'accepted'
    assert results['accepted']['reason'] == 'accepted'
    assert results['blocked']['reason'] == 'partial_application_blocked'
    assert results['timeout']['reason'] == 'timeout'

    cache_manager = ExperimentManager(
        repo_root=repo_root,
        evaluator=dynamic_evaluator,
        cache_path=cache_path,
    )
    cached_best, cached_results = asyncio.run(cache_manager.run_async([accepted]))

    assert cached_best is not None
    assert cached_best.id == 'accepted'
    assert cached_results['accepted']['accepted'] is True
    assert cached_results['accepted']['cached'] is True


def test_experiment_manager_reports_no_changes_applied_reason(monkeypatch, tmp_path: Path) -> None:
    _patch_cache_store(monkeypatch)
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    _prepare_repo(repo_root)
    existing_path = repo_root / 'src' / 'components' / 'existing.py'
    existing_path.write_text('x = 1\n', encoding='utf-8')

    manager = ExperimentManager(
        repo_root=repo_root,
        evaluator=_passing_evaluator,
        cache_path=repo_root / '.sif' / 'cache' / 'evals.json',
    )
    candidate = Candidate(
        id='no-op',
        source='test',
        code_changes=[CodeChange(path='src/components/existing.py', content='x = 1\n')],
    )

    best_candidate, results = asyncio.run(manager.run_async([candidate]))

    assert best_candidate is None
    assert results['no-op']['accepted'] is False
    assert results['no-op']['reason'] == 'no_changes_applied'


def test_experiment_manager_reports_evaluation_failed_for_non_dict_payload(monkeypatch, tmp_path: Path) -> None:
    _patch_cache_store(monkeypatch)
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    _prepare_repo(repo_root)

    async def invalid_payload_evaluator(_workspace: Path):
        return ['not', 'a', 'dict']

    manager = ExperimentManager(
        repo_root=repo_root,
        evaluator=invalid_payload_evaluator,
        cache_path=repo_root / '.sif' / 'cache' / 'evals.json',
    )
    candidate = Candidate(
        id='invalid-payload',
        source='test',
        code_changes=[CodeChange(path='src/components/generated_candidate.py', content='x = 3\n')],
    )

    best_candidate, results = asyncio.run(manager.run_async([candidate]))

    assert best_candidate is None
    assert results['invalid-payload']['accepted'] is False
    assert results['invalid-payload']['reason'] == 'evaluation_failed'


def test_experiment_manager_rejects_cached_malformed_metrics(monkeypatch, tmp_path: Path) -> None:
    _patch_cache_store(monkeypatch)
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    _prepare_repo(repo_root)
    cache_path = repo_root / '.sif' / 'cache' / 'evals.json'

    manager = ExperimentManager(
        repo_root=repo_root,
        evaluator=_passing_evaluator,
        cache_path=cache_path,
    )
    candidate = Candidate(
        id='cached-malformed',
        source='test',
        code_changes=[CodeChange(path='src/components/generated_candidate.py', content='x = 4\n')],
    )

    _, first_results = asyncio.run(manager.run_async([candidate]))
    assert first_results['cached-malformed']['accepted'] is True

    cache_key = next(
        key for key in _InMemoryCacheStore._data if ':cached-malformed:' in key
    )
    cached_entry = _InMemoryCacheStore._data[cache_key]
    assert isinstance(cached_entry, dict)
    metrics = cached_entry.get('metrics')
    assert isinstance(metrics, dict)
    metrics.pop('tests_skipped', None)

    second_manager = ExperimentManager(
        repo_root=repo_root,
        evaluator=_passing_evaluator,
        cache_path=cache_path,
    )
    best_candidate, second_results = asyncio.run(second_manager.run_async([candidate]))

    assert best_candidate is None
    assert second_results['cached-malformed']['accepted'] is False
    assert second_results['cached-malformed']['reason'] == 'malformed_metrics'
    assert second_results['cached-malformed']['cached'] is True
    assert 'reason' not in metrics
