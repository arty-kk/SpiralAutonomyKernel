from __future__ import annotations

from typing import Any, Dict


def build_behavior_profile(
    previous_profile: Dict[str, Any] | None,
    feedback_metrics: Dict[str, Any],
    evaluation: Dict[str, Any],
) -> Dict[str, Any]:
    canonical_keys = (
        "accuracy_signal",
        "adaptation_signal",
        "goal_alignment_score",
        "goal_coverage_ratio",
    )
    baseline_defaults = {key: 1.0 for key in canonical_keys}

    def _coerce_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    previous_profile = previous_profile or {}
    raw_baseline = previous_profile.get("baseline")
    baseline_source = raw_baseline if isinstance(raw_baseline, dict) else {}
    baseline = {
        key: _coerce_float(baseline_source.get(key), baseline_defaults[key])
        for key in canonical_keys
    }
    current = {
        key: _coerce_float(feedback_metrics.get(key), 0.0)
        for key in canonical_keys
    }
    deviations = {
        key: current[key] - baseline[key]
        for key in canonical_keys
    }
    deviation_flags = {
        key: abs(value) >= 0.2
        for key, value in deviations.items()
    }
    return {
        "baseline": baseline,
        "current": current,
        "deviations": deviations,
        "deviation_flags": deviation_flags,
        "alignment": evaluation.get("alignment"),
        "coverage": evaluation.get("coverage"),
    }
