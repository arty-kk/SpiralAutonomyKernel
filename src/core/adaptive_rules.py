from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Tuple

from sif.core.time_utils import utc_now_iso


@dataclass
class AdaptiveRulebook:
    revision_protocol: List[str]
    change_priorities: List[Dict[str, str]]
    meta_rules: List[Dict[str, str]]
    degradation_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            'revision_protocol': self.revision_protocol,
            'change_priorities': self.change_priorities,
            'meta_rules': self.meta_rules,
        }
        if self.degradation_notes:
            payload['degradation_notes'] = self.degradation_notes
        return payload


def build_default_rulebook() -> AdaptiveRulebook:
    return AdaptiveRulebook(
        revision_protocol=[
            'After a successful cycle, record metrics and review the rulebook.',
            'Rank change candidates by expected value and stability risk.',
            'Apply changes only with degradation checks and rollback readiness.',
            'Write results back to memory and revise effectiveness criteria.',
        ],
        change_priorities=[
            {'name': 'stability_guardrails', 'trigger': 'errors or reduced accuracy', 'rationale': 'protect stability before experimentation'},
            {'name': 'coverage_expansion', 'trigger': 'incomplete goal coverage', 'rationale': 'close identified gaps'},
            {'name': 'constraint_reframing', 'trigger': 'internal constraints are limiting progress', 'rationale': 'weaken unnecessary limits'},
            {'name': 'exploration_pressure', 'trigger': 'no new adaptations appear', 'rationale': 'stimulate new changes'},
        ],
        meta_rules=[
            {'name': 'tighten_success_criteria', 'condition': 'accuracy_signal < 1.0', 'adjustment': 'tighten success criteria for the next cycle'},
            {'name': 'loosen_exploration', 'condition': 'adaptation_signal == 0', 'adjustment': 'allow more experimental changes'},
            {'name': 'prioritize_coverage', 'condition': 'coverage != full', 'adjustment': 'increase the priority of coverage expansion'},
        ],
        degradation_notes=[],
    )


def load_rulebook(raw_rulebook: str | None) -> AdaptiveRulebook:
    default = build_default_rulebook()
    if not raw_rulebook:
        return default
    try:
        data = json.loads(raw_rulebook)
    except json.JSONDecodeError:
        return default

    degradation_notes: List[str] = []
    if not isinstance(data, dict):
        return AdaptiveRulebook(
            revision_protocol=default.revision_protocol,
            change_priorities=default.change_priorities,
            meta_rules=default.meta_rules,
            degradation_notes=['root: expected object at top level'],
        )

    revision_protocol = data.get('revision_protocol', default.revision_protocol)
    if not isinstance(revision_protocol, list):
        degradation_notes.append('revision_protocol: expected list of strings')
        revision_protocol = default.revision_protocol
    elif any(not isinstance(item, str) for item in revision_protocol):
        degradation_notes.append('revision_protocol: expected every item to be a string')
        revision_protocol = default.revision_protocol

    change_priorities = data.get('change_priorities', default.change_priorities)
    if not isinstance(change_priorities, list):
        degradation_notes.append('change_priorities: expected list of objects')
        change_priorities = default.change_priorities
    else:
        malformed = False
        for entry in change_priorities:
            if not isinstance(entry, dict):
                malformed = True
                degradation_notes.append('change_priorities: expected every item to be an object')
                break
            for key in ('name', 'trigger', 'rationale'):
                if key not in entry:
                    malformed = True
                    degradation_notes.append(f"change_priorities: missing required key '{key}'")
                    break
                if not isinstance(entry[key], str):
                    malformed = True
                    degradation_notes.append(f"change_priorities: key '{key}' must be a string")
                    break
            if malformed:
                break
        if malformed:
            change_priorities = default.change_priorities

    meta_rules = data.get('meta_rules', default.meta_rules)
    if not isinstance(meta_rules, list):
        degradation_notes.append('meta_rules: expected list of objects')
        meta_rules = default.meta_rules
    else:
        malformed = False
        for entry in meta_rules:
            if not isinstance(entry, dict):
                malformed = True
                degradation_notes.append('meta_rules: expected every item to be an object')
                break
            for key in ('name', 'condition', 'adjustment'):
                if key not in entry:
                    malformed = True
                    degradation_notes.append(f"meta_rules: missing required key '{key}'")
                    break
                if not isinstance(entry[key], str):
                    malformed = True
                    degradation_notes.append(f"meta_rules: key '{key}' must be a string")
                    break
            if malformed:
                break
        if malformed:
            meta_rules = default.meta_rules

    return AdaptiveRulebook(
        revision_protocol=revision_protocol,
        change_priorities=change_priorities,
        meta_rules=meta_rules,
        degradation_notes=degradation_notes,
    )


def _reorder_priorities(priorities: List[Dict[str, str]], preferred: List[str]) -> List[Dict[str, str]]:
    priority_map = {entry['name']: entry for entry in priorities}
    ordered: List[Dict[str, str]] = []
    for name in preferred:
        if name in priority_map:
            ordered.append(priority_map.pop(name))
    ordered.extend(priority_map.values())
    return ordered


def reconfigure_rulebook(
    rulebook: AdaptiveRulebook,
    evaluation: Dict[str, str],
    feedback_metrics: Dict[str, Any],
    cycle_index: int,
    change_log: List[Dict[str, Any]],
) -> Tuple[AdaptiveRulebook, List[Dict[str, Any]], Dict[str, Any]]:
    errors = evaluation.get('errors', [])
    accuracy_signal = feedback_metrics.get('accuracy_signal')
    adaptation_signal = feedback_metrics.get('adaptation_signal')
    coverage = evaluation.get('coverage')
    performance_stable = (not errors) and accuracy_signal == 1.0
    preferred_order: List[str] = []
    if errors or accuracy_signal != 1.0:
        preferred_order.append('stability_guardrails')
    if coverage != 'full':
        preferred_order.append('coverage_expansion')
    if adaptation_signal == 0:
        preferred_order.append('exploration_pressure')
    if not preferred_order:
        preferred_order.append('constraint_reframing')
    new_priorities = _reorder_priorities(rulebook.change_priorities, preferred_order)
    status = {
        'cycle': cycle_index,
        'performance_stable': performance_stable,
        'priority_order': [entry['name'] for entry in new_priorities],
        'criteria_adjustments': [],
    }
    if rulebook.degradation_notes:
        status['rulebook_degradation'] = {'detected': True, 'notes': list(rulebook.degradation_notes)}
        change_log.append({
            'timestamp': utc_now_iso(timespec='seconds'),
            'cycle': cycle_index,
            'action': 'rulebook_degradation_detected',
            'summary': 'Malformed rulebook sections were restored from defaults.',
            'notes': list(rulebook.degradation_notes),
        })
    else:
        status['rulebook_degradation'] = {'detected': False, 'notes': []}
    if errors or accuracy_signal != 1.0:
        status['criteria_adjustments'].append('tighten_success_criteria')
    if adaptation_signal == 0:
        status['criteria_adjustments'].append('loosen_exploration')
    if coverage != 'full':
        status['criteria_adjustments'].append('prioritize_coverage')
    if performance_stable:
        change_log.append({
            'timestamp': utc_now_iso(timespec='seconds'),
            'cycle': cycle_index,
            'action': 'rule_revision',
            'summary': 'Stable performance confirmed; rules re-ranked and criteria updated.',
            'priority_order': status['priority_order'],
            'criteria_adjustments': status['criteria_adjustments'],
        })
    else:
        change_log.append({
            'timestamp': utc_now_iso(timespec='seconds'),
            'cycle': cycle_index,
            'action': 'rule_revision_deferred',
            'summary': 'Performance instability detected; deferred aggressive rule changes.',
            'priority_order': status['priority_order'],
            'criteria_adjustments': status['criteria_adjustments'],
        })
    rulebook.change_priorities = new_priorities
    return rulebook, change_log, status
