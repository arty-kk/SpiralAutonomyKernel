# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass, field
from typing import Any, Dict, List

from sif.core.evolution import KernelUpdate
from sif.core.evolution import CodeChange


def evaluate_plan_coverage(plan: List[str], matched_items: List[str]) -> float:
    plan_relevant = [item for item in plan if item.strip()]
    if not plan_relevant:
        return 0.0
    matched_count = min(len({item for item in matched_items if item.strip()}), len(plan_relevant))
    coverage = matched_count / len(plan_relevant)
    return max(0.0, min(coverage, 1.0))


@dataclass
class ComponentSignal:
    component: str
    coverage: float | None = None
    risks: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    notes: str | None = None


def validate_plan(plan: List[str], component_tag: str) -> ComponentSignal | None:
    if plan:
        if any(not isinstance(item, str) for item in plan) or all(
            not isinstance(item, str) or not item.strip() for item in plan
        ):
            signal = ComponentSignal(component=component_tag)
            signal.errors.append("uninterpretable_plan")
            signal.risks.append(f"{component_tag}:uninterpretable_items")
            signal.notes = (
                f"{component_tag.replace('_', ' ').title()} review blocked: plan items were blank."
            )
            signal.coverage = 0.0
            return signal
        return None
    signal = ComponentSignal(component=component_tag)
    signal.errors.append("empty_plan")
    signal.risks.append(f"{component_tag}:no_plan_items")
    signal.notes = f"{component_tag.replace('_', ' ').title()} review skipped: plan is empty."
    signal.coverage = 0.0
    return signal


@dataclass
class Component:
    name: str

    async def apply(self, plan: List[str]) -> ComponentSignal:
        """Apply plan items to the component."""
        _ = plan
        return self.empty_signal()

    def empty_signal(self) -> ComponentSignal:
        return ComponentSignal(component=self.name)

    async def propose_updates(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection_summary: str,
    ) -> List[KernelUpdate]:
        _ = observations, evaluation, reflection_summary
        return []

    async def propose_code_changes(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection_summary: str,
    ) -> List[CodeChange]:
        _ = observations, evaluation, reflection_summary
        return []
