from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict


async def run_benchmarks_async(_repo_root: Path) -> Dict[str, Any]:
    from sif.core.kernel import Kernel, KernelState
    from sif.core.spiral_engine import SpiralEngine

    state = KernelState(goals=["Benchmark goal"], constraints=["Benchmark constraint"])
    kernel = Kernel(state=state)
    async with SpiralEngine(kernel=kernel) as engine:
        start = time.perf_counter()
        observations = engine.observe()
        observe_sec = time.perf_counter() - start

        original_request = engine.llm.request_response

        async def _noop_request_response(*_args, **_kwargs):
            return None

        engine.llm.request_response = _noop_request_response
        try:
            start = time.perf_counter()
            plan = await engine._plan_impl(observations=observations)
            plan_sec = time.perf_counter() - start

            start = time.perf_counter()
            evaluation = await engine._evaluate_impl(observations=observations, signals=[])
            reflect = await engine._reflect_impl(evaluation=evaluation)
            reflect_sec = time.perf_counter() - start
        finally:
            engine.llm.request_response = original_request

    return {
        "observe_sec": observe_sec,
        "plan_sec": plan_sec,
        "reflect_sec": reflect_sec,
        "plan_length": len(plan),
        "reflection_summary": reflect.summary,
    }
