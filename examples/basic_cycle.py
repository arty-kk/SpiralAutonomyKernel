from __future__ import annotations

import asyncio
import json

from sif.core.kernel import KernelState, Kernel
from sif.core.spiral_engine import SpiralEngine


async def main() -> None:
    kernel = Kernel(state=KernelState(goals=["Sustain bounded self-improvement"], constraints=["Maintain rollback readiness"], memory={}))
    async with SpiralEngine(kernel=kernel) as engine:
        result = await engine.step()
    print(json.dumps({
        "plan": result.plan,
        "evaluation": result.evaluation,
        "reflection": result.reflection.summary,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
