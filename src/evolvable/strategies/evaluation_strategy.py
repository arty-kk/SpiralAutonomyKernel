from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from sif.core.spiral_engine import SpiralEngine
    from sif.components.base import ComponentSignal


@dataclass
class EvaluationStrategy:
    name: str = "base_evaluation"

    async def evaluate(
        self,
        engine: "SpiralEngine",
        observations: Dict[str, str],
        signals: List["ComponentSignal"],
    ) -> Dict[str, Any]:
        raise NotImplementedError


@dataclass
class DefaultEvaluationStrategy(EvaluationStrategy):
    name: str = "default_evaluation"

    async def evaluate(
        self,
        engine: "SpiralEngine",
        observations: Dict[str, str],
        signals: List["ComponentSignal"],
    ) -> Dict[str, Any]:
        return await engine._evaluate_impl(observations=observations, signals=signals)


@dataclass
class ExperimentalEvaluationStrategy(EvaluationStrategy):
    name: str = "experimental_evaluation"

    async def evaluate(
        self,
        engine: "SpiralEngine",
        observations: Dict[str, str],
        signals: List["ComponentSignal"],
    ) -> Dict[str, Any]:
        _ = engine, observations
        return {
            "alignment": "partial",
            "coverage": "partial",
            "notes": "Experimental evaluation placeholder.",
            "errors": [],
            "metrics": {
                "signal_count": len(signals),
                "source": "experimental",
            },
        }
