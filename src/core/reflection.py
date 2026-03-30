from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from sif.core.time_utils import utc_now


@dataclass
class ConstraintAssessment:
    name: str
    classification: str
    notes: str


@dataclass
class ReflectionEntry:
    timestamp: datetime
    summary: str
    constraints: List[ConstraintAssessment] = field(default_factory=list)
    opportunities: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    ignored_directives: List[str] = field(default_factory=list)
    dod: Dict[str, Any] | None = None
    dod_check: Dict[str, Any] | None = None


@dataclass
class ReflectionLog:
    entries: List[ReflectionEntry] = field(default_factory=list)

    def add_entry(
        self,
        summary: str,
        constraints: List[ConstraintAssessment] | None = None,
        opportunities: List[str] | None = None,
        assumptions: List[str] | None = None,
        ignored_directives: List[str] | None = None,
        dod: Dict[str, Any] | None = None,
        dod_check: Dict[str, Any] | None = None,
    ) -> None:
        self.entries.append(
            ReflectionEntry(
                timestamp=utc_now(),
                summary=summary,
                constraints=constraints or [],
                opportunities=opportunities or [],
                assumptions=assumptions or [],
                ignored_directives=ignored_directives or [],
                dod=dod,
                dod_check=dod_check,
            )
        )

    def latest(self) -> ReflectionEntry | None:
        if not self.entries:
            return None
        return self.entries[-1]


@dataclass
class SelfCorrectionPlan:
    adjustments: Dict[str, str]
    goal_updates: List[str]
    rationale: str


def build_self_correction_plan(
    evaluation: Dict[str, Any],
    feedback_metrics: Dict[str, Any],
    current_strategy: str | None = None,
) -> SelfCorrectionPlan:
    alignment = evaluation.get('alignment')
    errors = evaluation.get('errors') or []
    accuracy_signal = float(feedback_metrics.get('accuracy_signal', 1.0) or 0.0)
    adaptation_signal = float(feedback_metrics.get('adaptation_signal', 1.0) or 0.0)
    adjustments: Dict[str, str] = {}
    goal_updates: List[str] = []
    rationale = 'No corrective action required.'
    if errors or alignment == 'at_risk' or accuracy_signal < 1.0:
        adjustments['auto_evolution_active_method'] = 'stability_guard'
        adjustments['active_plan_strategy'] = 'default_plan'
        goal_updates.append('Restore stable evaluation signals')
        rationale = 'Detected degradation; switching to stabilization posture.'
    elif adaptation_signal == 0.0:
        adjustments['auto_evolution_active_method'] = 'exploration_spark'
        adjustments['active_plan_strategy'] = 'experimental_plan'
        goal_updates.append('Reignite adaptation signals')
        rationale = 'Adaptation stalled; shifting to exploration.'
    if current_strategy and adjustments.get('active_plan_strategy') == current_strategy:
        adjustments.pop('active_plan_strategy', None)
    return SelfCorrectionPlan(
        adjustments=adjustments,
        goal_updates=goal_updates,
        rationale=rationale,
    )
