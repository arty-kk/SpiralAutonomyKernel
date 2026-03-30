from __future__ import annotations

from typing import Any, Dict, List


def generate_curriculum(
    goals: List[str],
    metrics: Dict[str, Any] | None,
    errors: List[str] | None,
) -> List[str]:
    tasks: List[str] = []
    metrics = metrics or {}
    errors = errors or []
    if errors:
        tasks.append("Resolve reported evaluation errors before new experiments.")
    goal_breakdown = metrics.get("goal_breakdown", {})
    if isinstance(goal_breakdown, dict):
        for goal in goals:
            goal_metrics = goal_breakdown.get(goal, {})
            coverage = goal_metrics.get("coverage")
            if isinstance(coverage, (int, float)) and coverage < 1.0:
                tasks.append(f"Improve coverage for goal: {goal}.")
    risks = metrics.get("risks", {})
    risk_items = risks.get("items") if isinstance(risks, dict) else []
    if isinstance(risk_items, list) and "coverage_gap" in risk_items:
        tasks.append("Close coverage gaps to reach full evaluation coverage.")
    if not tasks:
        tasks.append("Maintain stable improvements and verify new signals.")
    return tasks


def generate_goals(
    existing_goals: List[str],
    metrics: Dict[str, Any] | None,
    errors: List[str] | None,
) -> List[str]:
    metrics = metrics or {}
    errors = errors or []
    new_goals: List[str] = []
    if errors:
        new_goals.append("Eliminate recurring evaluation errors")
    goal_breakdown = metrics.get("goal_breakdown", {})
    if isinstance(goal_breakdown, dict):
        for goal, goal_metrics in goal_breakdown.items():
            coverage = goal_metrics.get("coverage")
            if isinstance(coverage, (int, float)) and coverage < 0.7:
                new_goals.append(f"Improve reliability for goal: {goal}")
    risks = metrics.get("risks", {})
    risk_items = risks.get("items") if isinstance(risks, dict) else []
    if isinstance(risk_items, list) and "coverage_gap" in risk_items:
        new_goals.append("Close systemic coverage gaps")
    filtered = [goal for goal in new_goals if goal not in existing_goals]
    return filtered
