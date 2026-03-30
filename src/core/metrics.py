from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from sif.core.reflection import ReflectionEntry


@dataclass
class ProgressMetricDefinition:
    weights: Dict[str, float]
    thresholds: Dict[str, float]
    revision_notes: list[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "weights": self.weights,
            "thresholds": self.thresholds,
            "revision_notes": self.revision_notes,
        }


def default_progress_metrics() -> ProgressMetricDefinition:
    return ProgressMetricDefinition(
        weights={
            "accuracy_signal": 0.4,
            "adaptation_signal": 0.3,
            "goal_alignment_score": 0.2,
            "goal_coverage_ratio": 0.1,
        },
        thresholds={
            "accuracy_signal": 0.9,
            "adaptation_signal": 0.5,
            "goal_alignment_score": 0.75,
            "goal_coverage_ratio": 0.7,
        },
        revision_notes=["Initial metric definition"],
    )


def revise_progress_metrics(
    current: Dict[str, Any] | None,
    reflection: ReflectionEntry | None,
    feedback_metrics: Dict[str, Any],
) -> ProgressMetricDefinition:
    if current:
        weights = current.get("weights") or {}
        thresholds = current.get("thresholds") or {}
        revision_notes = list(current.get("revision_notes") or [])
    else:
        default = default_progress_metrics()
        weights = default.weights
        thresholds = default.thresholds
        revision_notes = default.revision_notes

    accuracy_signal = float(feedback_metrics.get("accuracy_signal", 1.0) or 0.0)
    adaptation_signal = float(feedback_metrics.get("adaptation_signal", 1.0) or 0.0)
    if accuracy_signal < thresholds.get("accuracy_signal", 0.9):
        weights["accuracy_signal"] = min(weights.get("accuracy_signal", 0.4) + 0.1, 0.6)
        revision_notes.append("Increased accuracy weight after signal drop.")
    if adaptation_signal == 0.0:
        weights["adaptation_signal"] = min(weights.get("adaptation_signal", 0.3) + 0.1, 0.5)
        revision_notes.append("Boosted adaptation weight due to zero adaptation signal.")
    if reflection and reflection.dod_check and not reflection.dod_check.get("improved", True):
        thresholds["goal_alignment_score"] = max(
            thresholds.get("goal_alignment_score", 0.75) - 0.05,
            0.5,
        )
        revision_notes.append("Lowered alignment threshold after DoD miss.")
    return ProgressMetricDefinition(weights=weights, thresholds=thresholds, revision_notes=revision_notes)
