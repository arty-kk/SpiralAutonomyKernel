# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class MetaPlan:
    plan_strategy: str
    evaluation_strategy: str
    reflection_strategy: str
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_strategy": self.plan_strategy,
            "evaluation_strategy": self.evaluation_strategy,
            "reflection_strategy": self.reflection_strategy,
            "rationale": self.rationale,
        }


def select_strategies(feedback_metrics: Dict[str, Any]) -> MetaPlan:
    accuracy_signal = float(feedback_metrics.get("accuracy_signal", 1.0) or 0.0)
    adaptation_signal = float(feedback_metrics.get("adaptation_signal", 1.0) or 0.0)
    if accuracy_signal < 1.0:
        return MetaPlan(
            plan_strategy="default_plan",
            evaluation_strategy="default_evaluation",
            reflection_strategy="default_reflection",
            rationale="Accuracy dropped; prioritize stability.",
        )
    if adaptation_signal == 0.0:
        return MetaPlan(
            plan_strategy="experimental_plan",
            evaluation_strategy="experimental_evaluation",
            reflection_strategy="experimental_reflection",
            rationale="Adaptation stalled; shift to experimental strategies.",
        )
    return MetaPlan(
        plan_strategy="default_plan",
        evaluation_strategy="default_evaluation",
        reflection_strategy="default_reflection",
        rationale="Stable signals; keep default strategies.",
    )
