# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
import json
from typing import Any, Dict, List

from sif.core import async_cpu
from sif.core.bandit import (
    BanditState,
    evolve_state,
    load_bandit_state,
    select_action,
    serialize_bandit_state,
    update_state,
)
from sif.core.kernel import Kernel, KernelState
from sif.core.time_utils import utc_now_iso


@dataclass
class EvolutionMethod:
    name: str
    description: str
    trigger: str


def _load_history(raw_history: str | None) -> List[Dict[str, Any]]:
    """Load evolution history as a JSON list of dict entries.

    Expected input: JSON-encoded list where each item is a dict.
    Returns [] when the input is missing, invalid JSON, or has a mismatched format.
    """
    if not raw_history:
        return []
    try:
        decoded = json.loads(raw_history)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list) or any(not isinstance(item, dict) for item in decoded):
        return []
    return decoded


def _record_history(
    history: List[Dict[str, Any]],
    cycle_index: int,
    action: str,
    method: str | None,
    rationale: str,
    metrics: Dict[str, Any],
) -> List[Dict[str, Any]]:
    history.append(
        {
            "timestamp": utc_now_iso(timespec="seconds"),
            "cycle": cycle_index,
            "action": action,
            "method": method,
            "rationale": rationale,
            "metrics": metrics,
        }
    )
    return history


def _candidate_methods(
    evaluation: Dict[str, str],
    feedback_metrics: Dict[str, Any],
) -> List[EvolutionMethod]:
    coverage = evaluation.get("coverage", "partial")
    accuracy_signal = feedback_metrics.get("accuracy_signal", 0.0)
    adaptation_signal = feedback_metrics.get("adaptation_signal", 0.0)
    deltas = feedback_metrics.get("deltas")
    deltas = deltas if isinstance(deltas, dict) else {}

    def _delta_value(name: str) -> float | None:
        if name not in deltas:
            return None
        try:
            return float(deltas.get(name))
        except (TypeError, ValueError):
            return None

    alignment_delta = _delta_value("alignment_score_delta")
    goal_coverage_delta = _delta_value("goal_coverage_ratio_delta")
    coverage_average_delta = _delta_value("coverage_average_delta")
    delta_values = [
        value
        for value in [alignment_delta, goal_coverage_delta, coverage_average_delta]
        if value is not None
    ]
    delta_available = bool(delta_values)
    minimal_delta = delta_available and all(abs(value) < 0.01 for value in delta_values)
    negative_delta = delta_available and any(value < 0.0 for value in delta_values)
    positive_delta = delta_available and all(value > 0.0 for value in delta_values)
    methods = [
        EvolutionMethod(
            name="stability_guard",
            description="Strengthen validation before applying new changes.",
            trigger="accuracy_signal < 1.0",
        ),
        EvolutionMethod(
            name="coverage_scout",
            description="Systematically search for gaps in goal coverage.",
            trigger="coverage != full",
        ),
        EvolutionMethod(
            name="exploration_spark",
            description="Run small probes for new reasoning methods.",
            trigger="adaptation_signal == 0",
        ),
        EvolutionMethod(
            name="refinement_loop",
            description="Stabilize the current rules and improve accuracy.",
            trigger="default",
        ),
    ]
    if negative_delta:
        return [methods[0], methods[1], methods[3]]
    if minimal_delta:
        return [methods[1], methods[3]]
    if positive_delta:
        return [methods[3]]
    if accuracy_signal < 1.0:
        return [methods[0], methods[3]]
    if coverage != "full":
        return [methods[1], methods[3]]
    if adaptation_signal == 0:
        return [methods[2], methods[3]]
    return [methods[3]]


def _analysis_snapshot(
    evaluation: Dict[str, str],
    feedback_metrics: Dict[str, Any],
    history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    history_aggregates = _history_aggregates(history)
    return {
        "coverage": evaluation.get("coverage"),
        "errors": evaluation.get("errors", []),
        "accuracy_signal": feedback_metrics.get("accuracy_signal"),
        "adaptation_signal": feedback_metrics.get("adaptation_signal"),
        "history_aggregates": history_aggregates,
    }


def _ranked_candidates(
    candidates: List[EvolutionMethod],
    analysis: Dict[str, Any],
) -> List[Dict[str, Any]]:
    ranked = []
    history_aggregates = analysis.get("history_aggregates", {})
    for candidate in candidates:
        signals = dict(analysis)
        candidate_history = history_aggregates.get(
            candidate.name,
            {
                "avg_alignment_score_delta": 0.0,
                "avg_goal_coverage_ratio_delta": 0.0,
                "samples": 0,
            },
        )
        signals["history"] = candidate_history
        signals.pop("history_aggregates", None)
        ranked.append(
            {
                "name": candidate.name,
                "description": candidate.description,
                "trigger": candidate.trigger,
                "signals": signals,
            }
        )
    return ranked


def _select_method(
    candidates: List[EvolutionMethod],
    history: List[Dict[str, Any]],
    history_aggregates: Dict[str, Dict[str, float]],
    active_method: str | None,
) -> EvolutionMethod | None:
    recent = {entry.get("method") for entry in history[-3:] if entry.get("method")}
    candidates_with_priority = []
    for candidate in candidates:
        aggregate = history_aggregates.get(
            candidate.name,
            {
                "avg_alignment_score_delta": 0.0,
                "avg_goal_coverage_ratio_delta": 0.0,
                "samples": 0,
            },
        )
        has_positive_history = (
            aggregate.get("samples", 0) > 0
            and aggregate.get("avg_alignment_score_delta", 0.0) > 0.0
            and aggregate.get("avg_goal_coverage_ratio_delta", 0.0) > 0.0
        )
        candidates_with_priority.append((has_positive_history, candidate))
    candidates_with_priority.sort(key=lambda item: item[0], reverse=True)
    for _, candidate in candidates_with_priority:
        if candidate.name != active_method and candidate.name not in recent:
            return candidate
    return candidates[0] if candidates else None


def _safe_delta_value(metrics: Dict[str, Any], key: str) -> float:
    deltas = metrics.get("deltas")
    deltas = deltas if isinstance(deltas, dict) else {}
    try:
        return float(deltas.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _history_aggregates(
    history: List[Dict[str, Any]],
) -> Dict[str, Dict[str, float]]:
    aggregates: Dict[str, Dict[str, float]] = {}
    for entry in history:
        if entry.get("action") != "observe":
            continue
        method = entry.get("method")
        if not method:
            continue
        metrics = entry.get("metrics", {})
        try:
            alignment_delta = float(metrics.get("alignment_score_delta"))
        except (TypeError, ValueError):
            alignment_delta = None
        try:
            coverage_delta = float(metrics.get("goal_coverage_ratio_delta"))
        except (TypeError, ValueError):
            coverage_delta = None
        if alignment_delta is None and coverage_delta is None:
            continue
        aggregate = aggregates.setdefault(
            method,
            {
                "alignment_sum": 0.0,
                "coverage_sum": 0.0,
                "samples": 0,
            },
        )
        aggregate["alignment_sum"] += alignment_delta or 0.0
        aggregate["coverage_sum"] += coverage_delta or 0.0
        aggregate["samples"] += 1
    result: Dict[str, Dict[str, float]] = {}
    for method, aggregate in aggregates.items():
        samples = aggregate["samples"]
        if samples > 0:
            avg_alignment = aggregate["alignment_sum"] / samples
            avg_coverage = aggregate["coverage_sum"] / samples
        else:
            avg_alignment = 0.0
            avg_coverage = 0.0
        result[method] = {
            "avg_alignment_score_delta": avg_alignment,
            "avg_goal_coverage_ratio_delta": avg_coverage,
            "samples": samples,
        }
    return result


def _bandit_reward(feedback_metrics: Dict[str, Any]) -> float:
    accuracy_signal = float(feedback_metrics.get("accuracy_signal", 0.0) or 0.0)
    goal_alignment_score = float(feedback_metrics.get("goal_alignment_score", 0.0) or 0.0)
    goal_coverage_ratio = float(feedback_metrics.get("goal_coverage_ratio", 0.0) or 0.0)
    cycles_since_last_error = feedback_metrics.get("cycles_since_last_error")
    if cycles_since_last_error is None:
        stability_signal = 0.0
    else:
        try:
            stability_signal = min(max(float(cycles_since_last_error) / 10.0, 0.0), 1.0)
        except (TypeError, ValueError):
            stability_signal = 0.0
    accuracy_signal = min(max(accuracy_signal, 0.0), 1.0)
    goal_alignment_score = min(max(goal_alignment_score, 0.0), 1.0)
    goal_coverage_ratio = min(max(goal_coverage_ratio, 0.0), 1.0)
    reward = (
        0.4 * accuracy_signal
        + 0.3 * goal_alignment_score
        + 0.2 * goal_coverage_ratio
        + 0.1 * stability_signal
    )
    return min(max(reward, 0.0), 1.0)


def _load_bandit_state(kernel: Kernel) -> tuple[BanditState, bool]:
    raw_bandit = kernel.state.memory.get("bandit_state")
    return load_bandit_state(raw_bandit)


def run_autonomous_evolution(
    kernel: Kernel,
    cycle_index: int,
    evaluation: Dict[str, str],
    feedback_metrics: Dict[str, Any],
    observations: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    raw_history = kernel.state.memory.get("auto_evolution_log")
    history = _load_history(raw_history)
    if raw_history:
        invalid_history_format = False
        try:
            decoded_history = json.loads(raw_history)
        except json.JSONDecodeError:
            invalid_history_format = True
        else:
            invalid_history_format = not (
                isinstance(decoded_history, list)
                and all(isinstance(item, dict) for item in decoded_history)
            )
        if invalid_history_format:
            kernel.update_memory("auto_evolution_history_invalid_format", "true")
    active_method = kernel.state.memory.get("auto_evolution_active_method")
    previous_method = kernel.state.memory.get("auto_evolution_previous_method")
    raw_last_change_cycle = kernel.state.memory.get("auto_evolution_last_change_cycle", "0")
    try:
        last_change_cycle = int(raw_last_change_cycle)
    except (TypeError, ValueError) as exc:
        last_change_cycle = 0
        kernel.update_memory(
            "auto_evolution_last_change_cycle_parse_error",
            json.dumps(
                {
                    "timestamp": utc_now_iso(timespec="seconds"),
                    "exception": type(exc).__name__,
                    "value": raw_last_change_cycle,
                },
                ensure_ascii=False,
                default=str,
            ),
        )
    if last_change_cycle < 0:
        last_change_cycle = 0
    errors = evaluation.get("errors", [])
    accuracy_signal = feedback_metrics.get("accuracy_signal", 0.0)
    history_metrics = {
        "coverage": evaluation.get("coverage"),
        "accuracy_signal": feedback_metrics.get("accuracy_signal"),
        "adaptation_signal": feedback_metrics.get("adaptation_signal"),
        "goal_alignment_score": feedback_metrics.get("goal_alignment_score"),
        "goal_coverage_ratio": feedback_metrics.get("goal_coverage_ratio"),
        "alignment_score_delta": _safe_delta_value(feedback_metrics, "alignment_score_delta"),
        "goal_coverage_ratio_delta": _safe_delta_value(feedback_metrics, "goal_coverage_ratio_delta"),
    }
    context = dict(observations or {})
    context.update(
        {
            "accuracy_signal": feedback_metrics.get("accuracy_signal"),
            "goal_alignment_score": feedback_metrics.get("goal_alignment_score"),
            "goal_coverage_ratio": feedback_metrics.get("goal_coverage_ratio"),
            "cycles_since_last_error": feedback_metrics.get("cycles_since_last_error"),
        }
    )
    bandit_state, bandit_valid = _load_bandit_state(kernel)
    rollback_triggered = False
    action_taken = "observe"
    rationale = "Autonomy cycle completed without changing the active method."
    change = {
        "action": action_taken,
        "method": active_method,
        "rationale": rationale,
    }
    history_aggregates = _history_aggregates(history)

    if active_method and cycle_index > last_change_cycle:
        if errors or accuracy_signal < 1.0:
            rollback_triggered = True
            action_taken = "rollback"
            rationale = "Metrics degraded; rolling back the active method."
            fallback = previous_method or "baseline"
            kernel.update_memory("auto_evolution_active_method", fallback)
            kernel.update_memory("auto_evolution_previous_method", active_method)
            kernel.update_memory("auto_evolution_last_change_cycle", str(cycle_index))
            history = _record_history(
                history,
                cycle_index,
                action_taken,
                fallback,
                rationale,
                {"errors": errors, "accuracy_signal": accuracy_signal},
            )
            change = {
                "action": action_taken,
                "method": fallback,
                "rationale": rationale,
            }

    selected_method_name: str | None = None
    if not rollback_triggered:
        candidates = _candidate_methods(evaluation, feedback_metrics)
        candidate_names = [candidate.name for candidate in candidates]
        bandit_choice = None
        bandit_reason = None
        if bandit_valid:
            bandit_choice, bandit_reason = select_action(candidate_names, bandit_state)
        if bandit_choice:
            selected = next(
                (candidate for candidate in candidates if candidate.name == bandit_choice),
                None,
            )
            selected_method_name = bandit_choice
        else:
            selected = _select_method(candidates, history, history_aggregates, active_method)
            selected_method_name = selected.name if selected else None
            if bandit_reason:
                rationale = f"{rationale} Bandit fallback: {bandit_reason}."
        if selected and selected.name != active_method:
            kernel.update_memory("auto_evolution_previous_method", active_method or "baseline")
            kernel.update_memory("auto_evolution_active_method", selected.name)
            kernel.update_memory("auto_evolution_last_change_cycle", str(cycle_index))
            action_taken = "activate"
            rationale = f"Activated method: {selected.description}"
            history = _record_history(
                history,
                cycle_index,
                action_taken,
                selected.name,
                rationale,
                history_metrics,
            )
            change = {
                "action": action_taken,
                "method": selected.name,
                "rationale": rationale,
            }
        elif not selected and bandit_choice:
            change = {
                "action": action_taken,
                "method": active_method,
                "rationale": f"{rationale} Bandit suggested an unknown method {bandit_choice}.",
            }

    if action_taken == "observe":
        history = _record_history(
            history,
            cycle_index,
            action_taken,
            active_method,
            rationale,
            history_metrics,
        )

    reward_value = _bandit_reward(feedback_metrics)
    successful_cycle = not errors and accuracy_signal == 1.0
    reward_method = kernel.state.memory.get("auto_evolution_active_method") or selected_method_name
    if successful_cycle and reward_method:
        bandit_state = update_state(bandit_state, reward_method, reward_value)
    bandit_state = evolve_state(bandit_state, reward_value)
    kernel.update_memory("bandit_state", serialize_bandit_state(bandit_state))
    kernel.update_memory("auto_evolution_log", json.dumps(history, ensure_ascii=False))
    analysis = _analysis_snapshot(evaluation, feedback_metrics, history)
    report = {
        "cycle": cycle_index,
        "active_method": kernel.state.memory.get("auto_evolution_active_method"),
        "action": action_taken,
        "rollback": rollback_triggered,
        "rationale": rationale,
        "loop": {
            "observation": analysis,
            "analysis": {
                "candidates": _ranked_candidates(
                    _candidate_methods(evaluation, feedback_metrics),
                    analysis,
                ),
                "active_method": active_method,
                "context": context,
            },
            "change": change,
            "verification": "The next cycle will verify metrics and roll back on degradation.",
        },
        "verification_plan": "The next cycle will verify metrics and roll back on degradation.",
    }
    kernel.update_memory("auto_evolution_status", json.dumps(report, ensure_ascii=False))
    return report


def _autonomous_evolution_memory_updates(
    baseline_memory: Dict[str, str],
    evolved_memory: Dict[str, str],
) -> Dict[str, str]:
    updates: Dict[str, str] = {}
    for key, value in evolved_memory.items():
        if baseline_memory.get(key) != value:
            updates[key] = value
    return updates


async def run_autonomous_evolution_async(
    kernel: Kernel,
    cycle_index: int,
    evaluation: Dict[str, str],
    feedback_metrics: Dict[str, Any],
    observations: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    baseline_memory = dict(kernel.state.memory)
    shadow_kernel = Kernel(
        state=KernelState(
            goals=list(kernel.state.goals),
            constraints=list(kernel.state.constraints),
            memory=baseline_memory.copy(),
        )
    )
    report = await async_cpu.run_cpu(
        run_autonomous_evolution,
        shadow_kernel,
        cycle_index,
        evaluation,
        feedback_metrics,
        observations,
    )
    kernel.update_memory_many(
        _autonomous_evolution_memory_updates(baseline_memory, shadow_kernel.state.memory)
    )
    return report
