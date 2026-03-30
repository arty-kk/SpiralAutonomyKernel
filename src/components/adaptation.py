from dataclasses import dataclass
from typing import Any, Dict, List

from sif.components.base import Component, ComponentSignal, evaluate_plan_coverage, validate_plan
from sif.core.evolution import KernelUpdate


@dataclass
class AdaptationComponent(Component):
    async def apply(self, plan: List[str]) -> ComponentSignal:
        signal = validate_plan(plan, self.name)
        if signal is not None:
            return signal
        signal = self.empty_signal()
        keywords = ['adapt', 'explore', 'refine', 'iteration', 'coverage', 'capability', 'improve', 'experiment', 'expand']
        relevant_items = [item for item in plan if any(keyword in item.lower() for keyword in keywords)]
        matched_items = list(relevant_items)
        if not relevant_items:
            signal.risks.append('adaptation:no_relevant_plan_items')
        key_actions = {
            'coverage_expansion': ['coverage', 'expand'],
            'exploration': ['explore', 'experiment'],
            'refinement': ['refine', 'improve', 'iteration'],
        }
        for action, markers in key_actions.items():
            if not any(marker in item.lower() for marker in markers for item in plan):
                signal.risks.append(f'adaptation:missing_{action}')
        processed_summary = ', '.join(relevant_items) if relevant_items else 'none'
        signal.notes = f'Adaptation processed items: {processed_summary}.'
        signal.coverage = evaluate_plan_coverage(relevant_items, matched_items)
        return signal

    async def propose_updates(self, observations: Dict[str, str], evaluation: Dict[str, Any], reflection_summary: str) -> List[KernelUpdate]:
        updates: List[KernelUpdate] = []
        goals = {goal.strip() for goal in observations.get('goals', '').split(',') if goal.strip()}
        constraints = [constraint.strip() for constraint in observations.get('constraints', '').split(',') if constraint.strip()]
        if evaluation.get('coverage') == 'partial' and 'Expand capability coverage' not in goals:
            updates.append(KernelUpdate(action='add_goal', target='goals', value='Expand capability coverage', notes='Close capability gaps identified during evaluation.'))
        for constraint in constraints:
            if constraint.lower().startswith(('temporary:', 'internal:')):
                updates.append(KernelUpdate(action='remove_constraint', target='constraints', value=constraint, notes='Remove explicitly temporary or internal limits when safe.'))
        updates.append(KernelUpdate(action='update_memory', target='adaptation_reflection', value=f'summary={reflection_summary}', notes='Persist the adaptation signal for the next cycle.'))
        return updates
