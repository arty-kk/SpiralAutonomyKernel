from __future__ import annotations

from typing import Any, Dict, List

from sif.core.reflection import ReflectionEntry


def build_autonomy_charter(
    observations: Dict[str, str],
    evaluation: Dict[str, Any],
    reflection: ReflectionEntry,
    feedback_metrics: Dict[str, Any],
    static_report: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    config = config or {}
    goals = [goal.strip() for goal in observations.get('goals', '').split(',') if goal.strip()]
    constraints = [constraint.strip() for constraint in observations.get('constraints', '').split(',') if constraint.strip()]
    external_constraints = [constraint for constraint in constraints if constraint.lower().startswith('external:')]
    internal_constraints = [constraint for constraint in constraints if constraint not in external_constraints]
    coverage_full = evaluation.get('coverage') == 'full' and not evaluation.get('errors')
    alignment = evaluation.get('alignment', 'unknown')
    charter_status = 'stable' if coverage_full and alignment == 'stable' else 'reassess'

    autonomy_principles = config.get('autonomy_principles')
    if not isinstance(autonomy_principles, list):
        autonomy_principles = [
            'The runtime may improve itself only inside explicit policy boundaries.',
            'Internal limits are reviewable when they block measurable improvement.',
            'Self-modification is valid only when it remains observable and recoverable.',
            'Reflection is required before widening the next cycle scope.',
        ]

    self_modification_scope = config.get('self_modification_scope')
    if not isinstance(self_modification_scope, list):
        self_modification_scope = [
            'goals and constraints',
            'memory schema and long-term traces',
            'component registry and evaluation signals',
            'rulebooks and autonomy directives',
            'generated code surfaces inside allowed paths',
        ]

    next_actions: List[str] = []
    if not coverage_full:
        next_actions.append('Expand evaluation coverage before widening the next autonomy step.')
    if evaluation.get('errors'):
        next_actions.append('Eliminate current errors before applying broader self-modification.')
    if reflection.dod_check and reflection.dod_check.get('rollback_triggered'):
        next_actions.append('Recalibrate DoD signals before the next autonomy expansion attempt.')
    if not next_actions:
        next_actions.append('Maintain the current autonomy cadence and monitor for the next bounded improvement opportunity.')
    if static_report.get('risks'):
        next_actions.append('Resolve static-analysis risks that reduce confidence in self-modification safety.')

    improvement_signals = [
        {'name': 'autonomy_policy_refreshed', 'description': 'Autonomy policy updated with current goals, constraints, and signals.'},
        {'name': 'self_modification_scope_declared', 'description': 'The self-modification scope is explicit and reviewed for gaps.'},
        {'name': 'goal_alignment_score', 'description': 'Goal alignment score remains at or above 0.8.'},
        {'name': 'error_free_cycle', 'description': 'At least one full cycle completed with no evaluation errors.'},
    ]

    dod = {
        'target_aspect': 'Maintain bounded autonomy and explicit self-modification readiness.',
        'improvement_signals': improvement_signals,
        'assumptions_tested': [
            'Explicit self-modification scope increases operator clarity.',
            'Sustained error-free cycles indicate readiness for the next autonomy step.',
            'Goal alignment is required before increasing change scope.',
        ],
        'criteria_adequacy': [
            'Signals tie autonomy decisions to observable evaluation metrics.',
            'Scope declaration prevents hidden mutation boundaries.',
            'Alignment thresholds keep autonomy goal-directed.',
        ],
        'rollback_criteria': [
            'Coverage remains partial or evaluation errors persist.',
            'Goal alignment score drops below 0.8.',
            'Static-analysis risks grow unchecked.',
        ],
    }

    return {
        'status': charter_status,
        'identity_statement': 'The runtime is a persistent self-improvement system operating inside explicit policy boundaries.',
        'autonomy_principles': autonomy_principles,
        'self_modification_scope': self_modification_scope,
        'constraints': {'external': external_constraints, 'internal': internal_constraints},
        'goals_snapshot': goals,
        'evaluation_snapshot': {
            'alignment': alignment,
            'coverage': evaluation.get('coverage'),
            'errors': evaluation.get('errors', []),
            'goal_alignment_score': evaluation.get('metrics', {}).get('alignment_score', 0.0),
        },
        'feedback_snapshot': {
            'accuracy_signal': feedback_metrics.get('accuracy_signal'),
            'adaptation_signal': feedback_metrics.get('adaptation_signal'),
            'cycles_since_last_error': feedback_metrics.get('cycles_since_last_error'),
        },
        'next_actions': next_actions,
        'dod': dod,
    }
