# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sif.core.evolution import CodeApplicationResult, CodeChange
from sif.core.kernel import Kernel, KernelState
from sif.core.spiral_engine import SpiralEngine
import sif.core.spiral_engine as spiral_engine


class _AcceptedManager:
    def __init__(self, *args, **kwargs) -> None:
        _ = args, kwargs

    async def run_async(self, candidates, baseline_metrics=None):
        _ = baseline_metrics
        candidate = candidates[0]
        return candidate, {
            candidate.id: {
                'metrics': {
                    'compile_success': True,
                    'tests_success': True,
                    'tests_skipped': False,
                    'duration_sec': 0.01,
                },
                'accepted': True,
                'reason': 'accepted',
            }
        }


def _build_engine() -> SpiralEngine:
    kernel = Kernel(state=KernelState(goals=['g'], constraints=['c'], memory={'cycle_index': '1'}))
    return SpiralEngine(kernel=kernel)


def test_evolve_rolls_back_to_lkg_on_post_apply_degradation(monkeypatch, tmp_path: Path) -> None:
    engine = _build_engine()
    engine.kernel.update_memory('lkg_version_id', 'lkg-1')

    async def fake_create_version_async(*args, **kwargs):
        _ = args, kwargs
        return 'pre-1'

    async def fake_apply_code_changes_to_root_async(*args, **kwargs):
        _ = args, kwargs
        return CodeApplicationResult(
            applied_changes=[CodeChange(path='src/components/generated.py', content='x = 1\n')],
            blocked_changes=[],
        )

    async def fake_evaluate(*args, **kwargs):
        _ = args, kwargs
        return {'compile_success': False, 'tests_success': False, 'tests_skipped': False}

    async def fake_restore_version_async(version_id: str, mode: str = 'soft') -> bool:
        _ = mode
        return version_id == 'lkg-1'

    async def fake_latest_version_async() -> str | None:
        return 'fallback-1'

    async def fake_exists(path: Path) -> bool:
        return path.name == 'lkg-1'

    async def fake_load_version_paths_async(_version_id: str) -> list[str]:
        return ['src/components/generated.py']

    async def fake_append_event(*args, **kwargs):
        _ = args, kwargs
        return None

    monkeypatch.setattr(spiral_engine, 'ExperimentManager', _AcceptedManager)
    monkeypatch.setattr(spiral_engine, 'create_version_async', fake_create_version_async)
    monkeypatch.setattr(spiral_engine, 'apply_code_changes_to_root_async', fake_apply_code_changes_to_root_async)
    monkeypatch.setattr(spiral_engine, 'evaluate', fake_evaluate)
    monkeypatch.setattr(spiral_engine, 'restore_version_async', fake_restore_version_async)
    monkeypatch.setattr(spiral_engine, 'latest_version_async', fake_latest_version_async)
    monkeypatch.setattr(spiral_engine.async_fs, 'exists', fake_exists)
    monkeypatch.setattr(spiral_engine, 'append_event', fake_append_event)
    monkeypatch.setattr(engine, '_load_version_paths_async', fake_load_version_paths_async)
    monkeypatch.setattr(spiral_engine, 'REPO_ROOT', tmp_path)

    _, applied = asyncio.run(
        engine.evolve(
            evaluation={'alignment': 'ok'},
            updates=[],
            code_changes=[CodeChange(path='src/components/generated.py', content='x = 1\n')],
        )
    )

    assert applied == []
    assert engine.kernel.state.memory['rollback_triggered'] == 'true'
    assert engine.kernel.state.memory['rollback_reason'] == 'post_apply_compile_failed'
    assert engine.kernel.state.memory['rollback_version_id'] == 'lkg-1'
    rollback_info = json.loads(engine.kernel.state.memory['rollback_info'])
    assert rollback_info['restore_success'] is True
    assert rollback_info['restored_version_id'] == 'lkg-1'


def test_evolve_uses_latest_version_fallback_when_lkg_missing(monkeypatch, tmp_path: Path) -> None:
    engine = _build_engine()
    engine.kernel.update_memory('lkg_version_id', 'lkg-missing')

    async def fake_create_version_async(*args, **kwargs):
        _ = args, kwargs
        return 'pre-2'

    async def fake_apply_code_changes_to_root_async(*args, **kwargs):
        _ = args, kwargs
        return CodeApplicationResult(
            applied_changes=[CodeChange(path='src/components/generated.py', content='x = 2\n')],
            blocked_changes=[],
        )

    async def fake_evaluate(*args, **kwargs):
        _ = args, kwargs
        return {'compile_success': True, 'tests_success': False, 'tests_skipped': False}

    async def fake_restore_version_async(version_id: str, mode: str = 'soft') -> bool:
        _ = mode
        return version_id == 'fallback-2'

    async def fake_latest_version_async() -> str | None:
        return 'fallback-2'

    async def fake_exists(_path: Path) -> bool:
        return False

    async def fake_load_version_paths_async(_version_id: str) -> list[str]:
        return ['src/components/generated.py']

    async def fake_append_event(*args, **kwargs):
        _ = args, kwargs
        return None

    monkeypatch.setattr(spiral_engine, 'ExperimentManager', _AcceptedManager)
    monkeypatch.setattr(spiral_engine, 'create_version_async', fake_create_version_async)
    monkeypatch.setattr(spiral_engine, 'apply_code_changes_to_root_async', fake_apply_code_changes_to_root_async)
    monkeypatch.setattr(spiral_engine, 'evaluate', fake_evaluate)
    monkeypatch.setattr(spiral_engine, 'restore_version_async', fake_restore_version_async)
    monkeypatch.setattr(spiral_engine, 'latest_version_async', fake_latest_version_async)
    monkeypatch.setattr(spiral_engine.async_fs, 'exists', fake_exists)
    monkeypatch.setattr(spiral_engine, 'append_event', fake_append_event)
    monkeypatch.setattr(engine, '_load_version_paths_async', fake_load_version_paths_async)
    monkeypatch.setattr(spiral_engine, 'REPO_ROOT', tmp_path)

    _, applied = asyncio.run(
        engine.evolve(
            evaluation={'alignment': 'ok'},
            updates=[],
            code_changes=[CodeChange(path='src/components/generated.py', content='x = 2\n')],
        )
    )

    assert applied == []
    assert engine.kernel.state.memory['rollback_triggered'] == 'true'
    assert engine.kernel.state.memory['rollback_version_id'] == 'fallback-2'
    assert engine.kernel.state.memory['lkg_version_fallback'] == 'latest_version'
    rollback_info = json.loads(engine.kernel.state.memory['rollback_info'])
    assert rollback_info['fallback_restore_attempted'] is True
    assert rollback_info['fallback_restore_ok'] is True


def test_evolve_sets_rollback_failed_when_restore_paths_unavailable(monkeypatch, tmp_path: Path) -> None:
    engine = _build_engine()
    engine.kernel.update_memory('lkg_version_id', 'lkg-missing')

    async def fake_create_version_async(*args, **kwargs):
        _ = args, kwargs
        return 'pre-3'

    async def fake_apply_code_changes_to_root_async(*args, **kwargs):
        _ = args, kwargs
        return CodeApplicationResult(
            applied_changes=[CodeChange(path='src/components/generated.py', content='x = 4\n')],
            blocked_changes=[],
        )

    async def fake_evaluate(*args, **kwargs):
        _ = args, kwargs
        return {'compile_success': False, 'tests_success': False, 'tests_skipped': False}

    async def fake_restore_version_async(version_id: str, mode: str = 'soft') -> bool:
        _ = version_id, mode
        return False

    async def fake_latest_version_async() -> str | None:
        return None

    async def fake_exists(_path: Path) -> bool:
        return False

    async def fake_append_event(*args, **kwargs):
        _ = args, kwargs
        return None

    monkeypatch.setattr(spiral_engine, 'ExperimentManager', _AcceptedManager)
    monkeypatch.setattr(spiral_engine, 'create_version_async', fake_create_version_async)
    monkeypatch.setattr(spiral_engine, 'apply_code_changes_to_root_async', fake_apply_code_changes_to_root_async)
    monkeypatch.setattr(spiral_engine, 'evaluate', fake_evaluate)
    monkeypatch.setattr(spiral_engine, 'restore_version_async', fake_restore_version_async)
    monkeypatch.setattr(spiral_engine, 'latest_version_async', fake_latest_version_async)
    monkeypatch.setattr(spiral_engine.async_fs, 'exists', fake_exists)
    monkeypatch.setattr(spiral_engine, 'append_event', fake_append_event)
    monkeypatch.setattr(spiral_engine, 'REPO_ROOT', tmp_path)

    _, applied = asyncio.run(
        engine.evolve(
            evaluation={'alignment': 'ok'},
            updates=[],
            code_changes=[CodeChange(path='src/components/generated.py', content='x = 4\n')],
        )
    )

    assert applied != []
    assert engine.kernel.state.memory['rollback_triggered'] == 'true'
    assert engine.kernel.state.memory['rollback_failed'] == 'true'
    assert engine.kernel.state.memory['rollback_failure_reason'] == 'lkg_version_not_found_and_fallback_unavailable'
