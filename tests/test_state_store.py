from __future__ import annotations

from pathlib import Path

from sif.core.kernel import KernelState
from sif.core.state_store import load_state, save_state


def test_state_store_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / 'state.json'
    state = KernelState(
        goals=['Sustain bounded autonomous self-improvement'],
        constraints=['Maintain rollback readiness'],
        memory={'phase': 'smoke'},
    )

    import asyncio

    asyncio.run(save_state(state_path, state))
    loaded = asyncio.run(load_state(state_path))

    assert loaded.goals == state.goals
    assert loaded.constraints == state.constraints
    assert loaded.memory == state.memory
