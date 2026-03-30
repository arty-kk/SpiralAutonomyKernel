from __future__ import annotations

from pathlib import Path

from sif.core import versioning


def test_version_snapshot_and_restore(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    (repo_root / 'app.py').write_text('value = 1\n', encoding='utf-8')

    monkeypatch.setenv('SIF_REPO_ROOT', str(repo_root))
    import asyncio

    version_id = asyncio.run(versioning.create_version_async(snapshot_id='smoke-snapshot'))

    (repo_root / 'app.py').write_text('value = 2\n', encoding='utf-8')
    restored = asyncio.run(versioning.restore_version_async(version_id))

    assert restored is True
    assert (repo_root / 'app.py').read_text(encoding='utf-8') == 'value = 1\n'
