from __future__ import annotations

from typing import Any, Dict, List

from sif.core.reflection import ReflectionEntry


def build_autonomy_upgrade(
    static_report: Dict[str, Any],
    evaluation: Dict[str, Any],
    reflection: ReflectionEntry,
    feedback_metrics: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    config = config or {}
    gaps: List[str] = []
    actions: List[str] = []

    required_coverage = config.get("required_coverage", "full")
    if evaluation.get("coverage") != required_coverage:
        gaps.append("Coverage is not full; autonomy cannot rely on incomplete signals.")
        actions.append("Increase component coverage to stabilize autonomy signals.")
    if evaluation.get("errors"):
        gaps.append("Errors detected; autonomy needs error remediation before expanding.")
        actions.append("Prioritize error remediation and validation for self-evolution.")
    for risk in static_report.get("risks", []):
        gaps.append(risk)
        if "tests" in risk.lower():
            actions.append("Expand automated tests to protect autonomous changes.")
        if "generated components" in risk.lower():
            actions.append("Seed generated components to widen self-evolution channels.")
    if reflection.dod_check and reflection.dod_check.get("rollback_triggered"):
        gaps.append("DoD rollback triggered; autonomy must stabilize before expanding.")
        actions.append("Stabilize DoD signals and revalidate before further autonomy.")

    if not actions:
        actions.append("Maintain autonomy trajectory and monitor stability signals.")

    improvement_signals = config.get("improvement_signals")
    if not isinstance(improvement_signals, list):
        improvement_signals = [
            {
                "name": "coverage_full",
                "description": "Evaluation coverage reaches full with no errors.",
            },
            {
                "name": "feedback_accuracy_stable",
                "description": "Feedback accuracy signal remains stable across cycles.",
            },
            {
                "name": "self_evolution_channels_ready",
                "description": "Generated component pipeline and static analysis signals are active.",
            },
        ]

    dod = {
        "target_aspect": "Advance autonomous self-evolution and self-governance readiness.",
        "improvement_signals": improvement_signals,
        "assumptions_tested": [
            "Stable coverage and feedback accuracy allow safe autonomy expansion.",
            "Generated component channels are required for controlled self-modification.",
        ],
        "criteria_adequacy": [
            "Coverage and feedback stability provide observable readiness signals.",
            "Static analysis reveals structural gaps before autonomy escalates.",
        ],
        "rollback_criteria": [
            "Coverage remains partial or errors persist.",
            "Feedback accuracy signal degrades across cycles.",
        ],
    }

    summary = "Autonomy upgrade planned with gaps addressed." if gaps else "Autonomy stable."

    return {
        "summary": summary,
        "gaps": gaps,
        "actions": actions,
        "feedback_snapshot": {
            "accuracy_signal": feedback_metrics.get("accuracy_signal"),
            "adaptation_signal": feedback_metrics.get("adaptation_signal"),
        },
        "static_summary": static_report.get("summary"),
        "dod": dod,
        "config_applied": {
            "required_coverage": required_coverage,
            "custom_improvement_signals": bool(config.get("improvement_signals")),
        },
    }
