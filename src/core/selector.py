# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Mapping, Tuple


def should_accept(
    candidate_metrics: Mapping[str, Any],
    baseline_metrics: Mapping[str, Any] | None = None,
) -> Tuple[bool, str]:
    compile_success = candidate_metrics.get("compile_success") is True
    tests_success = candidate_metrics.get("tests_success") is True
    tests_skipped = bool(candidate_metrics.get("tests_skipped", False))
    if baseline_metrics is not None:
        baseline_compile = bool(baseline_metrics.get("compile_success", False))
        baseline_tests = bool(baseline_metrics.get("tests_success", False))
        if baseline_compile and not compile_success:
            return False, "compile_regression"
        if baseline_tests and (not tests_success or tests_skipped):
            return False, "tests_regression"
    if not compile_success:
        return False, "compile_failed"
    if tests_skipped:
        return False, "tests_skipped"
    if not tests_success:
        return False, "tests_failed"
    return True, "accepted"


def prioritize_focus(
    state: str,
    risks: Mapping[str, Any] | None = None,
) -> list[str]:
    risks = risks or {}
    risk_items = risks.get("items") if isinstance(risks, dict) else []
    priorities: list[str] = []
    if state == "recovering":
        priorities.append("stability_guardrails")
    elif state == "stabilizing":
        priorities.append("stability_guardrails")
        priorities.append("constraint_reframing")
    elif state == "exploring":
        priorities.append("exploration_pressure")
    else:
        priorities.append("coverage_expansion")
    if isinstance(risk_items, list) and "coverage_gap" in risk_items:
        priorities.insert(0, "coverage_expansion")
    seen: set[str] = set()
    ordered: list[str] = []
    for priority in priorities:
        if priority not in seen:
            ordered.append(priority)
            seen.add(priority)
    return ordered
