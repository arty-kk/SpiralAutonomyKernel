# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from sif.core.evolution import CodeChange, apply_code_changes_to_root_async


def test_apply_code_changes_allows_component_paths(tmp_path: Path) -> None:
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()

    import asyncio

    result = asyncio.run(apply_code_changes_to_root_async(
        repo_root,
        [CodeChange(path='src/components/generated_demo.py', content='x = 1\n')],
    ))

    assert result.out_of_policy is False
    assert (repo_root / 'src/components/generated_demo.py').read_text(encoding='utf-8') == 'x = 1\n'


def test_apply_code_changes_blocks_core_paths(tmp_path: Path) -> None:
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()

    import asyncio

    result = asyncio.run(apply_code_changes_to_root_async(
        repo_root,
        [CodeChange(path='src/core/unsafe.py', content='x = 1\n')],
    ))

    assert result.out_of_policy is True
    assert not (repo_root / 'src/core/unsafe.py').exists()
