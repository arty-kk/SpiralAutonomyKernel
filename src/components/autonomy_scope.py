from dataclasses import dataclass
import json
from typing import Any, Dict, List

from sif.components.base import Component, ComponentSignal, evaluate_plan_coverage, validate_plan
from sif.core.evolution import KernelUpdate


@dataclass
class AutonomyScopeComponent(Component):
    async def apply(self, plan: List[str]) -> ComponentSignal:
        signal = validate_plan(plan, self.name)
        if signal is not None:
            return signal
        signal = self.empty_signal()
        keywords = ['autonomy', 'scope', 'self-modification', 'self-evolution', 'policy', 'boundary']
        relevant_items = [item for item in plan if any(keyword in item.lower() for keyword in keywords)]
        matched_items = list(relevant_items)
        if not relevant_items:
            signal.risks.append('autonomy_scope:no_relevant_plan_items')
        key_actions = {
            'autonomy_refresh': ['autonomy', 'scope', 'boundary'],
            'self_modification_scope': ['self-modification', 'self-evolution', 'policy'],
        }
        for action, markers in key_actions.items():
            if not any(marker in item.lower() for marker in markers for item in plan):
                signal.risks.append(f'autonomy_scope:missing_{action}')
        processed_summary = ', '.join(relevant_items) if relevant_items else 'none'
        signal.notes = f'Autonomy scope processed items: {processed_summary}.'
        signal.coverage = evaluate_plan_coverage(relevant_items, matched_items)
        return signal

    async def propose_updates(self, observations: Dict[str, str], evaluation: Dict[str, Any], reflection_summary: str) -> List[KernelUpdate]:
        _ = evaluation
        updates: List[KernelUpdate] = []
        goals = [goal.strip() for goal in observations.get('goals', '').split(',') if goal.strip()]
        autonomy_goals = [
            'Maintain bounded autonomous self-improvement',
            'Keep self-modification explicit and reviewable',
            'Preserve recursive reflection and rollback readiness',
        ]
        for goal in autonomy_goals:
            if goal not in goals:
                updates.append(KernelUpdate(action='add_goal', target='goals', value=goal, notes='Keep autonomy scope explicit and bounded.'))
        report = {
            'autonomy_goals': autonomy_goals,
            'reflection_summary': reflection_summary,
            'assumption': 'Explicit autonomy scope improves runtime discipline.',
            'status': 'needs_refresh' if evaluation.get('coverage') != 'full' else 'stable',
        }
        updates.append(KernelUpdate(action='update_memory', target='autonomy_scope_report', value=json.dumps(report, ensure_ascii=False), notes='Capture the latest autonomy-scope status.'))
        return updates
