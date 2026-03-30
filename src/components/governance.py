# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from typing import Any, Dict, List

from sif.components.base import Component, ComponentSignal, evaluate_plan_coverage, validate_plan
from sif.core.evolution import KernelUpdate


@dataclass
class GovernanceComponent(Component):
    async def apply(self, plan: List[str]) -> ComponentSignal:
        signal = validate_plan(plan, self.name)
        if signal is not None:
            return signal
        signal = self.empty_signal()
        keywords = [
            "governance",
            "alignment",
            "risk",
            "stability",
            "guardrail",
            "compliance",
            "policy",
            "review",
            "audit",
        ]
        relevant_items = [
            item for item in plan if any(keyword in item.lower() for keyword in keywords)
        ]
        matched_items = list(relevant_items)
        if not relevant_items:
            signal.risks.append("governance:no_relevant_plan_items")
        key_actions = {
            "alignment_check": ["alignment", "goals"],
            "risk_review": ["risk", "guardrail", "stability"],
        }
        for action, markers in key_actions.items():
            if not any(marker in item.lower() for marker in markers for item in plan):
                signal.risks.append(f"governance:missing_{action}")
        processed_summary = ", ".join(relevant_items) if relevant_items else "none"
        signal.notes = f"Governance processed items: {processed_summary}."
        signal.coverage = evaluate_plan_coverage(relevant_items, matched_items)
        return signal

    async def propose_updates(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection_summary: str,
    ) -> List[KernelUpdate]:
        _ = observations
        return [
            KernelUpdate(
                action="update_memory",
                target="governance_last_review",
                value=f"alignment={evaluation.get('alignment')}; summary={reflection_summary}",
                notes="Record governance checkpoint for executive loop.",
            )
        ]
