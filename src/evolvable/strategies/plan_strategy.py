from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from sif.core.spiral_engine import SpiralEngine


@dataclass
class PlanStrategy:
    name: str = "base_plan"

    async def plan(
        self,
        engine: "SpiralEngine",
        observations: Dict[str, str],
        auto_evolution_report: Dict[str, Any] | None = None,
        adaptive_rulebook: Dict[str, Any] | None = None,
    ) -> List[str]:
        raise NotImplementedError


@dataclass
class DefaultPlanStrategy(PlanStrategy):
    name: str = "default_plan"

    async def plan(
        self,
        engine: "SpiralEngine",
        observations: Dict[str, str],
        auto_evolution_report: Dict[str, Any] | None = None,
        adaptive_rulebook: Dict[str, Any] | None = None,
    ) -> List[str]:
        return await engine._plan_impl(
            observations=observations,
            auto_evolution_report=auto_evolution_report,
            adaptive_rulebook=adaptive_rulebook,
        )


@dataclass
class ExperimentalPlanStrategy(PlanStrategy):
    name: str = "experimental_plan"

    async def plan(
        self,
        engine: "SpiralEngine",
        observations: Dict[str, str],
        auto_evolution_report: Dict[str, Any] | None = None,
        adaptive_rulebook: Dict[str, Any] | None = None,
    ) -> List[str]:
        _ = engine, auto_evolution_report, adaptive_rulebook
        summary = observations.get("self_profile", "no-profile")
        return [
            "Experimental plan: probe a single improvement with minimal risk.",
            f"Profile snapshot: {summary}",
        ]
