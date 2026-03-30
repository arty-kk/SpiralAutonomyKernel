from dataclasses import dataclass
import json
from typing import Any, Dict, List

from sif.components.base import Component, ComponentSignal, evaluate_plan_coverage, validate_plan
from sif.core.evolution import KernelUpdate


@dataclass
class MissionAlignmentComponent(Component):
    async def apply(self, plan: List[str]) -> ComponentSignal:
        signal = validate_plan(plan, self.name)
        if signal is not None:
            return signal
        signal = self.empty_signal()
        keywords = ['mission', 'goal', 'alignment', 'focus', 'capability', 'roadmap', 'priority']
        relevant_items = [item for item in plan if any(keyword in item.lower() for keyword in keywords)]
        matched_items = list(relevant_items)
        if not relevant_items:
            signal.risks.append('mission_alignment:no_relevant_plan_items')
        key_actions = {
            'goal_alignment': ['alignment', 'goal'],
            'focus_review': ['focus', 'priority', 'mission'],
        }
        for action, markers in key_actions.items():
            if not any(marker in item.lower() for marker in markers for item in plan):
                signal.risks.append(f'mission_alignment:missing_{action}')
        processed_summary = ', '.join(relevant_items) if relevant_items else 'none'
        signal.notes = f'Mission alignment processed items: {processed_summary}.'
        signal.coverage = evaluate_plan_coverage(relevant_items, matched_items)
        return signal

    async def propose_updates(self, observations: Dict[str, str], evaluation: Dict[str, Any], reflection_summary: str) -> List[KernelUpdate]:
        mission_goal = 'Improve the runtime through bounded, measurable self-evolution'
        operating_goal = 'Maintain autonomous improvement inside a sandbox with rollback readiness'
        reliability_goal = 'Preserve alignment, observability, and recoverability across cycles'
        goals = [goal.strip() for goal in observations.get('goals', '').split(',') if goal.strip()]
        alignment_map = self._build_alignment_map(goals, mission_goal, operating_goal, reliability_goal)
        focus_check = self._build_focus_check(observations, evaluation, reflection_summary)
        updates: List[KernelUpdate] = []
        for goal, notes in [
            (mission_goal, 'Make the mission explicit.'),
            (operating_goal, 'Maintain the operating objective explicitly.'),
            (reliability_goal, 'Anchor the reliability objective explicitly.'),
        ]:
            if goal not in goals:
                updates.append(KernelUpdate(action='add_goal', target='goals', value=goal, notes=notes))
        report = {
            'mission_goal': mission_goal,
            'operating_goal': operating_goal,
            'reliability_goal': reliability_goal,
            'alignment_map': alignment_map,
            'focus_check': focus_check,
        }
        updates.append(KernelUpdate(action='update_memory', target='mission_alignment_report', value=json.dumps(report, ensure_ascii=False), notes='Map local goals to the mission and check strategic focus.'))
        updates.append(KernelUpdate(action='update_memory', target='mission_alignment_status', value=focus_check['status'], notes='Store the latest strategic alignment status.'))
        return updates

    @staticmethod
    def _build_alignment_map(goals: List[str], mission_goal: str, operating_goal: str, reliability_goal: str) -> List[Dict[str, str]]:
        alignment_map: List[Dict[str, str]] = []
        for goal in goals:
            if goal in {mission_goal, operating_goal, reliability_goal}:
                continue
            alignment_map.append({'local_goal': goal, 'supports': mission_goal, 'rationale': MissionAlignmentComponent._goal_rationale(goal)})
        if not alignment_map:
            alignment_map.append({'local_goal': 'no-local-goals', 'supports': mission_goal, 'rationale': 'Establish local goals that directly support the mission.'})
        return alignment_map

    @staticmethod
    def _goal_rationale(goal: str) -> str:
        lowered = goal.lower()
        if 'coverage' in lowered:
            return 'Broadens capability coverage to sustain measurable improvement.'
        if 'evolution' in lowered or 'adapt' in lowered:
            return 'Improves the self-modification capacity of the runtime.'
        if 'rollback' in lowered or 'recover' in lowered:
            return 'Strengthens recoverability during autonomous changes.'
        return 'Contributes incremental capability toward the core mission.'

    @staticmethod
    def _build_focus_check(observations: Dict[str, str], evaluation: Dict[str, Any], reflection_summary: str) -> Dict[str, str | List[str]]:
        opportunities: List[str] = []
        if observations.get('external_constraints') == '0':
            opportunities.append('No external constraints were detected; validate whether the boundary model is complete.')
        if observations.get('internal_constraints') not in {'', '0', None}:
            opportunities.append('Internal constraints remain; prioritize review of the most limiting ones.')
        if evaluation.get('coverage') == 'partial':
            opportunities.append('Coverage gaps reveal capability areas that still need instrumentation.')
        if not opportunities:
            opportunities.append('Maintain the current mission framing and continue monitoring signals.')
        status = 'on_track' if evaluation.get('alignment') == 'stable' else 'reassess'
        return {
            'status': status,
            'evaluation_alignment': evaluation.get('alignment', 'unknown'),
            'evaluation_coverage': evaluation.get('coverage', 'unknown'),
            'reflection_summary': reflection_summary,
            'opportunities': opportunities,
        }
