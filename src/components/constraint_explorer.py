# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
import json
from typing import Any, Dict, List

from sif.components.base import Component, ComponentSignal, evaluate_plan_coverage, validate_plan
from sif.core import policy
from sif.core.evolution import KernelUpdate


@dataclass
class ConstraintExplorerComponent(Component):
    async def apply(self, plan: List[str]) -> ComponentSignal:
        signal = validate_plan(plan, self.name)
        if signal is not None:
            return signal
        signal = self.empty_signal()
        keywords = ['constraint', 'limit', 'option', 'reframe', 'assumption', 'dependency', 'boundary']
        relevant_items = [item for item in plan if any(keyword in item.lower() for keyword in keywords)]
        matched_items = list(relevant_items)
        if not relevant_items:
            signal.risks.append('constraint_explorer:no_relevant_plan_items')
        key_actions = {
            'constraint_reframing': ['constraint', 'limit', 'reframe'],
            'option_expansion': ['option', 'assumption', 'boundary'],
        }
        for action, markers in key_actions.items():
            if not any(marker in item.lower() for marker in markers for item in plan):
                signal.risks.append(f'constraint_explorer:missing_{action}')
        processed_summary = ', '.join(relevant_items) if relevant_items else 'none'
        signal.notes = f'Constraint explorer processed items: {processed_summary}.'
        signal.coverage = evaluate_plan_coverage(relevant_items, matched_items)
        return signal

    async def propose_updates(self, observations: Dict[str, str], evaluation: Dict[str, Any], reflection_summary: str) -> List[KernelUpdate]:
        _ = evaluation, reflection_summary
        updates: List[KernelUpdate] = []
        goals = {goal.strip() for goal in observations.get('goals', '').split(',') if goal.strip()}
        constraints = [constraint.strip() for constraint in observations.get('constraints', '').split(',') if constraint.strip()]
        protected_constraints = [constraint for constraint in constraints if not policy.can_remove_constraint(constraint)]
        hidden_constraints = [
            constraint
            for constraint in constraints
            if constraint not in protected_constraints
            and not constraint.lower().startswith(('external:', 'internal:', 'temporary:', 'conditional:', 'flexible:'))
        ]
        reframed_constraints = [f'flexible: {constraint} (review if broader options improve outcomes)' for constraint in hidden_constraints]
        if 'Increase the space of safe improvement options' not in goals:
            updates.append(KernelUpdate(action='add_goal', target='goals', value='Increase the space of safe improvement options', notes='Revisit the goal framing to widen bounded options.'))
        for constraint in hidden_constraints:
            updates.append(KernelUpdate(action='remove_constraint', target='constraints', value=constraint, notes='Remove an implicit internal constraint to expand bounded options.'))
        for constraint in reframed_constraints:
            updates.append(KernelUpdate(action='add_constraint', target='constraints', value=constraint, notes='Replace an implicit limit with an explicit reviewable condition.'))
        report = {
            'hidden_constraints': hidden_constraints,
            'reframed_constraints': reframed_constraints,
            'goal_adjustment': 'Increase the space of safe improvement options',
            'assumption': 'Reducing implicit limits can expand option space without losing control',
        }
        updates.append(KernelUpdate(action='update_memory', target='constraint_explorer_report', value=json.dumps(report, ensure_ascii=False), notes='Record constraint reframing and option expansion status.'))
        return updates
