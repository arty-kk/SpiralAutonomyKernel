# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

import ast
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from typing import Any, Dict, List
from pathlib import Path

from sif.core.adaptive_rules import load_rulebook, reconfigure_rulebook
from sif.core import async_fs
from sif.core.async_cpu import shutdown_cpu_executor, start_cpu_executor
from sif.core.async_fs import shutdown_fs_executor, start_fs_executor
from sif.core import policy
from sif.core.autonomy import build_autonomy_upgrade
from sif.core.autonomy_charter import build_autonomy_charter
from sif.core.autonomous_evolution import run_autonomous_evolution_async
from sif.core.bandit import load_bandit_state, suggest_focus
from sif.core.evolution import (
    CodeChange,
    KernelUpdate,
    apply_code_changes_to_root_async,
    apply_kernel_updates,
    rollback_code_changes_async,
    validate_code_changes,
    REPO_ROOT,
)
from sif.core.events import append_event, shutdown_event_writer, start_event_writer
from sif.core.evaluator import evaluate_async as evaluate
from sif.core.strategy_loader import load_strategy
from sif.core.candidates import Candidate
from sif.core.experiment_manager import ExperimentManager
from sif.core.code_intelligence import CodeIntelligence
from sif.core.versioning import (
    create_version_async,
    latest_version_async,
    restore_version_async,
)
from sif.core.tools.base import ToolCall, ToolManager, ToolPolicy
from sif.core.kernel import (
    MEMORY_SCHEMA_VERSION,
    METRICS_V1_NAMESPACE,
    REPORTS_V1_NAMESPACE,
    Kernel,
)
from sif.core.llm import LLMOrchestrator
from sif.core.reflection import ConstraintAssessment, ReflectionEntry, build_self_correction_plan
from sif.core.static_analysis import (
    analyze_repository_from_snapshot_async,
    build_python_repository_snapshot_async,
    build_self_map_from_snapshot_async,
    calculate_changed_python_files,
)
from sif.core.state_model import build_state_ontology
from sif.core.intent_graph import build_intent_graph
from sif.core.impact_ledger import append_impact_log, build_impact_entry
from sif.core.reports import build_behavior_profile
from sif.core.metrics import default_progress_metrics, revise_progress_metrics
from sif.core.selector import prioritize_focus
from sif.core.time_utils import utc_now, utc_now_iso
from sif.components.base import ComponentSignal
from sif.components.registry import ComponentRegistry
from sif.evolvable.curriculum import generate_curriculum, generate_goals
from sif.evolvable.strategies.meta_planner import select_strategies


@dataclass
class SpiralCycleResult:
    observations: Dict[str, str]
    plan: List[str]
    evaluation: Dict[str, Any]
    reflection: ReflectionEntry
    code_changes_applied: List[CodeChange]
    updates_applied: List[KernelUpdate]


@dataclass
class SpiralEngine:
    kernel: Kernel
    registry: ComponentRegistry = field(default_factory=ComponentRegistry)
    llm: LLMOrchestrator = field(default_factory=LLMOrchestrator)
    tool_manager: ToolManager = field(default_factory=ToolManager)
    llm_concurrency_limit: int = 3
    snapshot_io_concurrency_limit: int = 1
    component_hook_concurrency_limit: int = 4
    autonomous_evolution_concurrency_limit: int = 1

    def __post_init__(self) -> None:
        # Concurrency bounds used to protect external requests and heavy refresh work.
        self._llm_semaphore = asyncio.Semaphore(max(1, int(self.llm_concurrency_limit)))
        self._snapshot_io_semaphore = asyncio.Semaphore(max(1, int(self.snapshot_io_concurrency_limit)))
        self._component_hook_semaphore = asyncio.Semaphore(
            max(1, int(self.component_hook_concurrency_limit))
        )
        self._autonomous_evolution_semaphore = asyncio.Semaphore(
            max(1, int(self.autonomous_evolution_concurrency_limit))
        )
        self._refresh_pipeline_semaphore = asyncio.Semaphore(1)
        self._refresh_code_index_semaphore = asyncio.Semaphore(1)
        self._refresh_static_analysis_semaphore = asyncio.Semaphore(1)
        self._refresh_self_map_semaphore = asyncio.Semaphore(1)
        self._started = False

    async def _shutdown_session_resources(self) -> None:
        shutdown_error: BaseException | None = None
        for shutdown in (
            shutdown_event_writer,
            shutdown_fs_executor,
            shutdown_cpu_executor,
            self.llm.aclose,
        ):
            try:
                await shutdown()
            except BaseException as exc:
                if shutdown_error is None:
                    shutdown_error = exc
        if shutdown_error is not None:
            raise shutdown_error

    async def start(self) -> None:
        if self._started:
            return
        try:
            await self.llm.start()
            await start_fs_executor()
            await start_cpu_executor()
            await start_event_writer()
        except BaseException as startup_error:
            try:
                await self._shutdown_session_resources()
            except BaseException as shutdown_error:
                raise startup_error from shutdown_error
            raise
        self._started = True

    async def aclose(self) -> None:
        if not self._started:
            return
        try:
            await self._shutdown_session_resources()
        finally:
            self._started = False

    async def __aenter__(self) -> "SpiralEngine":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _run_llm_request_limited(
        self,
        task: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        async with self._llm_semaphore:
            return await self.llm.request_response(self.kernel, task, payload)

    async def _run_component_hook_limited(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        async with self._component_hook_semaphore:
            return await func(*args, **kwargs)

    async def _run_autonomous_evolution_limited(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        async with self._autonomous_evolution_semaphore:
            return await run_autonomous_evolution_async(*args, **kwargs)

    def observe(self) -> Dict[str, str]:
        constraint_counts = self._count_constraints(self.kernel.state.constraints)
        self_profile = self._build_self_profile()
        return {
            "goals": ", ".join(self.kernel.state.goals),
            "constraints": ", ".join(self.kernel.state.constraints),
            "memory_size": str(len(self.kernel.state.memory)),
            "internal_constraints": str(constraint_counts["internal"]),
            "external_constraints": str(constraint_counts["external"]),
            "self_profile": self_profile["summary"],
            "autonomy_charter_status": self.kernel.state.memory.get(
                "autonomy_charter_status", "unset"
            ),
        }

    async def use_tool(self, name: str, args: Dict[str, Any]) -> Any:
        raw_cycle_index = self.kernel.state.memory.get("cycle_index")
        cycle_index: int | None = None
        has_cycle_index = raw_cycle_index is not None
        if isinstance(raw_cycle_index, str):
            has_cycle_index = bool(raw_cycle_index.strip())
        elif isinstance(raw_cycle_index, (bytes, bytearray, list, tuple, dict, set, frozenset)):
            has_cycle_index = bool(raw_cycle_index)
        if has_cycle_index:
            try:
                cycle_index = int(raw_cycle_index)
            except (TypeError, ValueError) as exc:
                self.kernel.update_memory(
                    "cycle_index_parse_error",
                    json.dumps(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "exception": str(exc),
                            "value": raw_cycle_index,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                )
        return await self.tool_manager.call_tool(
            ToolCall(name=name, args=args),
            cycle_index=cycle_index,
        )

    async def plan(
        self,
        observations: Dict[str, str],
        auto_evolution_report: Dict[str, Any] | None = None,
        adaptive_rulebook: Dict[str, Any] | None = None,
    ) -> List[str]:
        strategy_name = self.kernel.state.memory.get("active_plan_strategy")
        strategy = load_strategy("plan", strategy_name)
        return await strategy.plan(
            self,
            observations=observations,
            auto_evolution_report=auto_evolution_report,
            adaptive_rulebook=adaptive_rulebook,
        )

    def _sync_tool_policy_from_memory(self) -> None:
        default_max_calls_per_cycle = 0
        default_max_runtime_sec = 0.0
        tool_policy_raw = self.kernel.state.memory.get("tool_policy")
        tool_policy: Dict[str, Any] | None = None
        if isinstance(tool_policy_raw, str):
            try:
                parsed = json.loads(tool_policy_raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                tool_policy = parsed
        elif isinstance(tool_policy_raw, dict):
            tool_policy = tool_policy_raw

        enabled_tools: dict[str, str] = {}
        if isinstance(tool_policy, dict):
            enabled_tools_raw = tool_policy.get("enabled_tools")
            if isinstance(enabled_tools_raw, list):
                for tool_entry in enabled_tools_raw:
                    if not isinstance(tool_entry, dict):
                        continue
                    tool_name = tool_entry.get("name")
                    documentation = tool_entry.get("documentation") or tool_entry.get("docs")
                    if isinstance(tool_name, str) and isinstance(documentation, str):
                        normalized_tool_name = tool_name.strip()
                        normalized_docs = documentation.strip()
                        if normalized_tool_name and normalized_docs:
                            enabled_tools[normalized_tool_name] = normalized_docs
            elif isinstance(enabled_tools_raw, dict):
                for tool_name, tool_entry in enabled_tools_raw.items():
                    if not isinstance(tool_name, str):
                        continue
                    documentation: Any | None
                    if isinstance(tool_entry, dict):
                        documentation = tool_entry.get("documentation") or tool_entry.get("docs")
                    elif isinstance(tool_entry, str):
                        documentation = tool_entry
                    else:
                        continue
                    if isinstance(documentation, str):
                        normalized_tool_name = tool_name.strip()
                        normalized_docs = documentation.strip()
                        if normalized_tool_name and normalized_docs:
                            enabled_tools[normalized_tool_name] = normalized_docs

        max_calls_per_cycle = default_max_calls_per_cycle
        max_runtime_sec = default_max_runtime_sec
        if isinstance(tool_policy, dict):
            raw_max_calls_per_cycle = tool_policy.get("max_calls_per_cycle")
            try:
                if isinstance(raw_max_calls_per_cycle, bool):
                    raise ValueError("Boolean values are not valid max_calls_per_cycle.")
                parsed_max_calls_per_cycle = int(raw_max_calls_per_cycle)
                if parsed_max_calls_per_cycle >= 0:
                    max_calls_per_cycle = parsed_max_calls_per_cycle
            except (TypeError, ValueError):
                max_calls_per_cycle = default_max_calls_per_cycle

            raw_max_runtime_sec = tool_policy.get("max_runtime_sec")
            try:
                if isinstance(raw_max_runtime_sec, bool):
                    raise ValueError("Boolean values are not valid max_runtime_sec.")
                parsed_max_runtime_sec = float(raw_max_runtime_sec)
                if parsed_max_runtime_sec >= 0 and math.isfinite(parsed_max_runtime_sec):
                    max_runtime_sec = parsed_max_runtime_sec
            except (TypeError, ValueError):
                max_runtime_sec = default_max_runtime_sec
        self.tool_manager.policy = ToolPolicy(
            enabled_tools=enabled_tools,
            max_calls_per_cycle=max_calls_per_cycle,
            max_runtime_sec=max_runtime_sec,
        )

    async def _plan_impl(
        self,
        observations: Dict[str, str],
        auto_evolution_report: Dict[str, Any] | None = None,
        adaptive_rulebook: Dict[str, Any] | None = None,
    ) -> List[str]:
        self._sync_tool_policy_from_memory()
        dod = self._build_dod(observations)
        self.kernel.update_memory(
            "last_cycle_dod",
            json.dumps(dod, ensure_ascii=False, sort_keys=True),
        )
        auto_evolution_report = auto_evolution_report or self._load_auto_evolution_report()
        adaptive_rulebook = adaptive_rulebook or self._load_adaptive_rulebook()
        active_method = (
            auto_evolution_report.get("active_method")
            if isinstance(auto_evolution_report, dict)
            else None
        ) or self.kernel.state.memory.get("auto_evolution_active_method")
        web_search_enabled = self.tool_manager.policy.is_enabled("web_search")
        rule_priorities = self._extract_rule_priorities(adaptive_rulebook)
        constraint_focus = "Remove internal limits and reframe assumptions"
        external_focus = "Map external constraints to explicit dependencies"
        if observations.get("internal_constraints") == "0":
            constraint_focus = "Identify new internal assumptions worth challenging"
        if observations.get("external_constraints") == "0":
            external_focus = "Seek new external opportunities without enforced limits"
        method_focus_map = {
            "stability_guard": "Prioritize stability guardrails and risk review.",
            "coverage_scout": "Emphasize coverage scouting before new changes.",
            "exploration_spark": "Seed exploration probes for new reasoning methods.",
            "refinement_loop": "Refine current logic and lock in stable wins.",
        }
        priority_action_map = {
            "stability_guardrails": "Stabilize current behavior before deeper changes.",
            "coverage_expansion": "Expand coverage for unmet goal areas.",
            "constraint_reframing": "Reframe internal constraints to unlock options.",
            "exploration_pressure": "Increase experimental probes for adaptation.",
        }
        method_focus = method_focus_map.get(active_method, "Maintain balanced improvement cadence.")
        bandit_focus = None
        charter_status = observations.get("autonomy_charter_status", "unset")
        bandit_state, bandit_valid = load_bandit_state(self.kernel.state.memory.get("bandit_state"))
        if bandit_valid:
            bandit_focus = suggest_focus(method_focus_map.keys(), bandit_state)
        if bandit_focus:
            bandit_focus_text = method_focus_map.get(
                bandit_focus, f"Increase focus on {bandit_focus}."
            )
            method_focus = f"{method_focus} Bandit focus: {bandit_focus_text}"
        priority_actions = [
            priority_action_map.get(name, f"Apply rule priority focus: {name}.")
            for name in rule_priorities
        ]
        if bandit_focus:
            bandit_priority = f"Bandit priority: {method_focus_map.get(bandit_focus, bandit_focus)}"
            if bandit_priority not in priority_actions:
                priority_actions.insert(0, bandit_priority)
        base_plan = [
            "Assess component performance",
            "Identify gaps against goals",
            constraint_focus,
            external_focus,
            "Explore bounded improvement options aligned with goals",
            "Propose incremental kernel updates",
        ]
        if web_search_enabled:
            base_plan.insert(
                base_plan.index("Propose incremental kernel updates"),
                "Use external web search to fill knowledge gaps and log findings",
            )
        if charter_status in {"unset", "expanding"}:
            base_plan.insert(
                2,
                "Refresh autonomy policy and the self-modification map for the next cycle.",
            )
        ordered_plan: List[str] = [method_focus]
        for action in priority_actions + base_plan:
            if action not in ordered_plan:
                ordered_plan.append(action)
        llm_plan = await self._resolve_llm_plan(
            observations=observations,
            method_focus=method_focus,
            priority_actions=priority_actions,
            base_plan=base_plan,
        )
        final_plan = llm_plan or ordered_plan
        if llm_plan is None:
            llm_error = self._load_last_llm_error_for_task("plan")
            if llm_error and llm_error.get("type") in {"timeout", "network_error", "llm_client_not_initialized"}:
                final_plan = list(final_plan)
                final_plan.append(f"LLM unavailable: {llm_error.get('type')}")
        self._update_curriculum_from_memory()
        return final_plan

    async def build(self, plan: List[str]) -> List[ComponentSignal]:
        tasks = [
            self._run_component_hook_limited(component.apply, plan)
            for component in self.registry.components
        ]
        return await asyncio.gather(*tasks) if tasks else []

    async def evaluate(
        self,
        observations: Dict[str, str],
        signals: List[ComponentSignal],
    ) -> Dict[str, Any]:
        strategy_name = self.kernel.state.memory.get("active_evaluation_strategy")
        strategy = load_strategy("evaluation", strategy_name)
        return await strategy.evaluate(self, observations=observations, signals=signals)

    async def _evaluate_impl(
        self,
        observations: Dict[str, str],
        signals: List[ComponentSignal],
    ) -> Dict[str, Any]:
        _ = observations
        coverage_values = [signal.coverage for signal in signals if signal.coverage is not None]
        if coverage_values:
            coverage_average = sum(coverage_values) / len(coverage_values)
            full_ratio = sum(1 for value in coverage_values if value >= 1.0) / len(coverage_values)
        else:
            coverage_average = 0.0
            full_ratio = 0.0
        combined_risks = [risk for signal in signals for risk in signal.risks]
        combined_errors = [error for signal in signals for error in signal.errors]
        combined_notes = [signal.notes for signal in signals if signal.notes]
        goals = list(self.kernel.state.goals)
        goal_metrics: Dict[str, Dict[str, Any]] = {}
        matched_goal_scores: List[float] = []
        covered_goal_scores: List[bool] = []
        for goal in goals:
            goal_key = goal.lower()
            matched_signals = [
                signal
                for signal in signals
                if (signal.notes and goal_key in signal.notes.lower())
                or (signal.component and goal_key in signal.component.lower())
            ]
            # Aggregate coverage as the average across matched signals to reflect overall progress.
            matched_coverages = [
                signal.coverage for signal in matched_signals if signal.coverage is not None
            ]
            coverage = (
                sum(matched_coverages) / len(matched_coverages)
                if matched_coverages
                else 0.0
            )
            evidence_entries: List[str] = []
            for signal in matched_signals:
                if signal.notes:
                    evidence_entries.append(signal.notes)
                elif signal.component:
                    evidence_entries.append(signal.component)
            evidence = list(dict.fromkeys(evidence_entries))
            risks = sorted({risk for signal in matched_signals for risk in signal.risks})
            goal_errors = [error for signal in matched_signals for error in signal.errors]
            status = "on_track"
            adjusted_coverage = coverage
            if goal_errors:
                status = "error"
                adjusted_coverage = 0.0
            elif risks:
                status = "at_risk"
                adjusted_coverage = max(coverage * 0.7, 0.0)
            elif coverage == 0.0:
                status = "no_signal"
            elif coverage < 1.0:
                status = "partial"
            goal_metrics[goal] = {
                "coverage": adjusted_coverage,
                "evidence": evidence,
                "risks": risks,
                "errors": goal_errors,
                "status": status,
            }
            status_score_map = {
                "on_track": 1.0,
                "partial": 0.5,
                "at_risk": 0.25,
                "no_signal": 0.0,
                "error": 0.0,
            }
            matched_goal_scores.append(status_score_map.get(status, 0.0))
            covered_goal_scores.append(bool(adjusted_coverage > 0 and evidence))
        if goals:
            alignment_score = sum(matched_goal_scores) / len(goals)
            goal_coverage_ratio = sum(1 for covered in covered_goal_scores if covered) / len(goals)
        else:
            alignment_score = 1.0
            goal_coverage_ratio = 1.0
        adjusted_coverage_average = coverage_average
        if combined_errors:
            adjusted_coverage_average = min(adjusted_coverage_average, 0.25)
        elif combined_risks:
            adjusted_coverage_average = min(adjusted_coverage_average, 0.75)
        coverage_label = (
            "full"
            if adjusted_coverage_average >= 0.95 and not combined_risks and not combined_errors
            else "partial"
        )
        if coverage_label != "full":
            combined_risks.append("coverage_gap")
        risks_excluding_coverage_gap = [
            risk for risk in combined_risks if risk != "coverage_gap"
        ]
        if combined_errors:
            alignment_label = "at_risk"
        elif risks_excluding_coverage_gap:
            alignment_label = "partial" if alignment_score >= 0.5 else "at_risk"
        elif alignment_score >= 0.8:
            alignment_label = "stable"
        elif alignment_score >= 0.5:
            alignment_label = "partial"
        else:
            alignment_label = "at_risk"
        evaluation = {
            "alignment": alignment_label,
            "coverage": coverage_label,
            "notes": "No regressions detected",
        }
        evaluation["errors"] = combined_errors
        evaluation["metrics"] = {
            "coverage": {
                "average": adjusted_coverage_average,
                "full_ratio": full_ratio,
                "reported": len(coverage_values),
                "total": len(signals),
            },
            "alignment_score": alignment_score,
            "goal_coverage_ratio": goal_coverage_ratio,
            "goal_breakdown": goal_metrics,
            "evidence": combined_notes,
            "risks": {
                "items": combined_risks,
                "count": len(combined_risks),
            },
            "errors": {
                "items": combined_errors,
                "count": len(combined_errors),
            },
        }
        if combined_errors:
            evaluation["notes"] = "Errors detected in component signals"
        elif evaluation["alignment"] == "partial":
            evaluation["notes"] = "Partial alignment across goals"
        elif evaluation["alignment"] == "at_risk":
            evaluation["notes"] = "Alignment at risk across goals"
        llm_evaluation = await self._resolve_llm_evaluation(
            observations=observations,
            signals=signals,
            fallback=evaluation,
        )
        if llm_evaluation is not None:
            return llm_evaluation
        llm_error = self._load_last_llm_error_for_task("evaluate")
        if llm_error and llm_error.get("type") in {"timeout", "network_error", "llm_client_not_initialized"}:
            evaluation["llm_error"] = {
                "type": llm_error.get("type"),
                "message": llm_error.get("message"),
                "task": "evaluate",
            }
        return evaluation

    async def reflect(self, evaluation: Dict[str, Any]) -> ReflectionEntry:
        strategy_name = self.kernel.state.memory.get("active_reflection_strategy")
        strategy = load_strategy("reflection", strategy_name)
        return await strategy.reflect(self, evaluation=evaluation)

    async def _reflect_impl(self, evaluation: Dict[str, Any]) -> ReflectionEntry:
        constraint_assessments = self.assess_constraints()
        opportunities = self.identify_opportunities(constraint_assessments)
        assumptions = self.identify_assumptions(constraint_assessments)
        internal_constraints = sum(
            1 for assessment in constraint_assessments if assessment.classification == "internal"
        )
        external_constraints = len(constraint_assessments) - internal_constraints
        constraint_names = ", ".join(assessment.name for assessment in constraint_assessments) or "none"
        summary = (
            "Reflection: alignment stable; coverage partial; "
            f"constraints={constraint_names}; "
            f"internal constraints={internal_constraints}; "
            f"external constraints={external_constraints}; "
            "next cycle should prioritize removing internal limits and expanding capabilities."
        )
        dod = self._load_last_dod()
        dod_check = self._check_dod(
            dod=dod,
            evaluation=evaluation,
            internal_constraints=internal_constraints,
            external_constraints=external_constraints,
            constraint_assessments=constraint_assessments,
            opportunities=opportunities,
            assumptions=assumptions,
        )
        llm_reflection = await self._resolve_llm_reflection(
            observations=self.observe(),
            evaluation=evaluation,
            reflection_summary=summary,
            constraints=constraint_assessments,
            opportunities=opportunities,
            assumptions=assumptions,
            dod=dod,
            dod_check=dod_check,
        )
        reflection_payload = llm_reflection or {
            "summary": summary,
            "constraints": constraint_assessments,
            "opportunities": opportunities,
            "assumptions": assumptions,
            "ignored_directives": [],
            "dod": dod,
            "dod_check": dod_check,
        }
        if llm_reflection is None:
            llm_error = self._load_last_llm_error_for_task("reflect")
            if llm_error and llm_error.get("type") in {"timeout", "network_error", "llm_client_not_initialized"}:
                reflection_payload["summary"] = (
                    f"{reflection_payload['summary']} | LLM unavailable: {llm_error.get('type')}"
                )
        self.kernel.record_reflection(
            reflection_payload["summary"],
            constraints=reflection_payload["constraints"],
            opportunities=reflection_payload["opportunities"],
            assumptions=reflection_payload["assumptions"],
            ignored_directives=reflection_payload["ignored_directives"],
            dod=reflection_payload["dod"],
            dod_check=reflection_payload["dod_check"],
        )
        return self.kernel.reflections.latest()

    async def decide_updates(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection: ReflectionEntry,
    ) -> List[KernelUpdate]:
        if self._safe_mode_active():
            return [
                KernelUpdate(
                    action="update_memory",
                    target="safe_mode_status",
                    value="safe_mode_noop",
                    notes="Safe mode active; skipping update proposals.",
                )
            ]
        proposals: List[KernelUpdate] = []
        metrics_payload = (
            evaluation.get("metrics") if isinstance(evaluation.get("metrics"), dict) else {}
        )
        new_goals = generate_goals(
            existing_goals=self.kernel.state.goals,
            metrics=metrics_payload,
            errors=evaluation.get("errors"),
        )
        for goal in new_goals:
            proposals.append(
                KernelUpdate(
                    action="add_goal",
                    target="goals",
                    value=goal,
                    notes="Generated by Goal Genesis Engine.",
                )
            )
        update_tasks = [
            self._run_component_hook_limited(
                component.propose_updates,
                observations=observations,
                evaluation=evaluation,
                reflection_summary=reflection.summary,
            )
            for component in self.registry.components
        ]
        component_update_sets = await asyncio.gather(*update_tasks) if update_tasks else []
        for component_updates in component_update_sets:
            proposals.extend(component_updates)
        if not proposals:
            proposals.append(
                KernelUpdate(
                    action="update_memory",
                    target="executive_decision",
                    value="no-op",
                    notes="No component proposals; preserve current kernel state.",
                )
            )
        return proposals

    async def decide_code_changes(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection: ReflectionEntry,
    ) -> List[CodeChange]:
        if os.getenv("SIF_EVALUATION_CONTEXT") == "1":
            return []
        if self._safe_mode_active():
            return []
        proposals: List[CodeChange] = []
        llm_changes = await self._resolve_llm_code_changes(
            observations=observations,
            evaluation=evaluation,
            reflection=reflection,
        )
        proposals.extend(llm_changes)
        code_change_tasks = [
            self._run_component_hook_limited(
                component.propose_code_changes,
                observations=observations,
                evaluation=evaluation,
                reflection_summary=reflection.summary,
            )
            for component in self.registry.components
        ]
        component_change_sets = await asyncio.gather(*code_change_tasks) if code_change_tasks else []
        for component_changes in component_change_sets:
            proposals.extend(component_changes)
        return proposals

    async def evolve(
        self,
        evaluation: Dict[str, Any],
        updates: List[KernelUpdate],
        code_changes: List[CodeChange],
    ) -> tuple[List[KernelUpdate], List[CodeChange]]:
        self.kernel.update_memory(
            "last_evaluation",
            json.dumps(evaluation, ensure_ascii=False),
        )
        latest_reflection = self.kernel.reflections.latest()
        if latest_reflection:
            self.kernel.update_memory("last_reflection_summary", latest_reflection.summary)
            self.kernel.update_memory(
                "last_constraint_assessment",
                ", ".join(
                    f"{assessment.name}:{assessment.classification}"
                    for assessment in latest_reflection.constraints
                ),
            )
        applied_code_changes: List[CodeChange] = []
        candidate_metrics: Dict[str, Any] | None = None
        rolled_back_post_apply = False
        if code_changes:
            candidates = [Candidate(code_changes=code_changes, source="composite")]
            baseline_metrics = None
            raw_lkg_metrics = self.kernel.state.memory.get("lkg_metrics")
            if raw_lkg_metrics:
                try:
                    parsed = json.loads(raw_lkg_metrics)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    baseline_metrics = parsed
            manager = ExperimentManager(evaluator=evaluate)
            best_candidate, results = await manager.run_async(candidates, baseline_metrics=baseline_metrics)
            self.kernel.update_memory(
                "experiment_results",
                json.dumps(results, ensure_ascii=False),
            )
            for candidate_id, result in results.items():
                await append_event(
                    "candidate_evaluated",
                    {
                        "cycle_index": self.kernel.state.memory.get("cycle_index"),
                        "candidate_id": candidate_id,
                        "metrics": result.get("metrics"),
                    },
                )
                await append_event(
                    "candidate_decision",
                    {
                        "cycle_index": self.kernel.state.memory.get("cycle_index"),
                        "candidate_id": candidate_id,
                        "accepted": result.get("accepted"),
                        "reason": result.get("reason"),
                    },
                )
            accepted = False
            rejection_reason = "no_acceptable_candidate"
            if best_candidate and best_candidate.id in results:
                candidate_metrics = results[best_candidate.id].get("metrics")
                accepted = bool(results[best_candidate.id].get("accepted"))
                rejection_reason = results[best_candidate.id].get("reason", rejection_reason)
            self.kernel.update_memory(
                "code_candidate_metrics",
                json.dumps(candidate_metrics, ensure_ascii=False) if candidate_metrics else "{}",
            )
            self.kernel.update_memory(
                "code_candidate_decision",
                "accepted" if accepted else "rejected",
            )
            self.kernel.update_memory(
                "code_candidate_reason",
                rejection_reason,
            )
            self.kernel.update_memory(
                "last_code_validation",
                "passed" if accepted else "rejected",
            )
            if not accepted:
                self.kernel.update_memory(
                    "last_code_validation_reason",
                    rejection_reason,
                )
            if accepted and best_candidate:
                pre_apply_version_id = None
                pre_apply_snapshot_error = None
                try:
                    pre_apply_version_id = await create_version_async()
                except Exception as exc:
                    pre_apply_snapshot_error = f"pre_apply_snapshot_failed:{exc}"
                if not pre_apply_version_id:
                    if not pre_apply_snapshot_error:
                        pre_apply_snapshot_error = "pre_apply_snapshot_missing_id"
                    self.kernel.update_memory(
                        "last_code_validation_reason",
                        pre_apply_snapshot_error,
                    )
                    self.kernel.update_memory(
                        "last_code_changes_applied",
                        "skipped_pre_apply_snapshot_failure",
                    )
                else:
                    self.kernel.update_memory(
                        "last_pre_apply_version_id",
                        pre_apply_version_id,
                    )
                    code_application_result = await apply_code_changes_to_root_async(
                        REPO_ROOT,
                        best_candidate.code_changes,
                        kernel=self.kernel,
                    )
                    applied_code_changes = code_application_result.applied_changes
                    await append_event(
                        "code_changes_applied",
                        {
                            "cycle_index": self.kernel.state.memory.get("cycle_index"),
                            "applied_paths": [change.path for change in applied_code_changes],
                            "requested_paths": [
                                change.path for change in best_candidate.code_changes
                            ],
                            "blocked_changes": [
                                {
                                    "path": blocked.path,
                                    "requested_path": blocked.requested_path,
                                    "reason": blocked.reason,
                                }
                                for blocked in code_application_result.blocked_changes
                            ],
                        },
                    )
                if applied_code_changes:
                    post_apply_metrics = await evaluate(REPO_ROOT)
                    lkg_metrics = None
                    raw_lkg_metrics = self.kernel.state.memory.get("lkg_metrics")
                    if raw_lkg_metrics:
                        try:
                            parsed = json.loads(raw_lkg_metrics)
                        except json.JSONDecodeError:
                            parsed = None
                        if isinstance(parsed, dict):
                            lkg_metrics = parsed
                    degradation_reason = self._detect_post_apply_degradation(
                        baseline_metrics=candidate_metrics or {},
                        post_metrics=post_apply_metrics,
                        lkg_metrics=lkg_metrics,
                    )
                    if degradation_reason:
                        lkg_version_id = self.kernel.state.memory.get("lkg_version_id")
                        lkg_restore_ok = False
                        lkg_restore_attempted = False
                        fallback_restore_ok = False
                        fallback_restore_attempted = False
                        restore_success = False
                        restored_version_id = None
                        fallback_version_id = None
                        lkg_version_path = None
                        if lkg_version_id:
                            lkg_version_path = (
                                REPO_ROOT / ".sif" / "versions" / lkg_version_id
                            )
                        if lkg_version_id and lkg_version_path and await async_fs.exists(lkg_version_path):
                            lkg_restore_attempted = True
                            lkg_restore_ok = await restore_version_async(lkg_version_id, mode="soft") is True
                            if lkg_restore_ok:
                                restored_version_id = lkg_version_id
                        if not lkg_restore_ok:
                            fallback_version_id = await latest_version_async()
                            if fallback_version_id:
                                fallback_restore_attempted = True
                                fallback_restore_ok = (
                                    await restore_version_async(fallback_version_id, mode="soft") is True
                                )
                                if fallback_restore_ok:
                                    restored_version_id = fallback_version_id
                            self.kernel.update_memory(
                                "lkg_version_fallback",
                                "latest_version",
                            )
                        restore_success = lkg_restore_ok or fallback_restore_ok
                        restored_paths: list[str] = []
                        if restore_success and restored_version_id:
                            restored_paths = await self._load_version_paths_async(restored_version_id)
                        rollback_failure_reason = None
                        if not restore_success:
                            if lkg_restore_attempted and fallback_restore_attempted:
                                rollback_failure_reason = (
                                    "restore_version_returned_false_for_lkg_and_fallback"
                                )
                            elif lkg_restore_attempted and not fallback_version_id:
                                rollback_failure_reason = (
                                    "restore_version_returned_false_for_lkg_fallback_missing"
                                )
                            elif not lkg_restore_attempted and fallback_restore_attempted:
                                rollback_failure_reason = "restore_version_returned_false_for_fallback"
                            elif lkg_version_id and not (
                                lkg_version_path and await async_fs.exists(lkg_version_path)
                            ):
                                rollback_failure_reason = "lkg_version_not_found_and_fallback_unavailable"
                            else:
                                rollback_failure_reason = "lkg_and_fallback_versions_unavailable"
                        rollback_info = {
                            "reason": degradation_reason,
                            "timestamp": utc_now_iso(timespec="seconds"),
                            "pre_apply_version_id": pre_apply_version_id,
                            "lkg_version_id": lkg_version_id,
                            "lkg_restore_attempted": lkg_restore_attempted,
                            "lkg_restore_ok": lkg_restore_ok,
                            "fallback_version_id": fallback_version_id,
                            "fallback_restore_attempted": fallback_restore_attempted,
                            "fallback_restore_ok": fallback_restore_ok,
                            "restore_success": restore_success,
                            "restored_version_id": restored_version_id,
                            "restored_paths": restored_paths,
                        }
                        if rollback_failure_reason:
                            rollback_info["rollback_failure_reason"] = rollback_failure_reason
                        self.kernel.update_memory("rollback_triggered", "true")
                        self.kernel.update_memory("rollback_reason", degradation_reason)
                        if restore_success and restored_version_id:
                            self.kernel.state.memory.pop("rollback_failed", None)
                            self.kernel.state.memory.pop("rollback_failure_reason", None)
                            self.kernel.update_memory("rollback_version_id", restored_version_id)
                        else:
                            self.kernel.state.memory.pop("rollback_version_id", None)
                            self.kernel.update_memory("rollback_failed", "true")
                            self.kernel.update_memory(
                                "rollback_failure_reason",
                                rollback_failure_reason,
                            )
                        self.kernel.update_memory(
                            "rollback_restored_paths",
                            json.dumps(restored_paths, ensure_ascii=False),
                        )
                        self.kernel.update_memory(
                            "rollback_info",
                            json.dumps(rollback_info, ensure_ascii=False),
                        )
                        await append_event(
                            "post_apply_rollback",
                            {
                                "cycle_index": self.kernel.state.memory.get("cycle_index"),
                                "reason": degradation_reason,
                                "version_id": restored_version_id,
                                "restored_paths": restored_paths,
                                "restore_success": restore_success,
                            },
                        )
                        if restore_success:
                            applied_code_changes = []
                            rolled_back_post_apply = True
                            self.kernel.update_memory(
                                "last_code_changes_applied",
                                "rolled_back_post_apply",
                            )
                    else:
                        version_id = await create_version_async()
                        self.kernel.update_memory("lkg_version_id", version_id)
                        self.kernel.update_memory(
                            "lkg_metrics",
                            json.dumps(post_apply_metrics, ensure_ascii=False),
                        )
                        self.kernel.update_memory(
                            "lkg_timestamp",
                            utc_now_iso(timespec="seconds"),
                        )
                        self.kernel.update_memory(
                            "last_version_snapshot",
                            json.dumps(
                                {
                                    "version_id": version_id,
                                    "timestamp": utc_now().isoformat(
                                        timespec="seconds"
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                        )
                    if not rolled_back_post_apply:
                        self.kernel.update_memory(
                            "last_code_changes_applied",
                            "applied_to_repo: "
                            + "; ".join(change.path for change in applied_code_changes),
                        )
                else:
                    self.kernel.update_memory(
                        "last_code_changes_applied",
                        "applied_to_repo: no-op",
                    )
            else:
                self.kernel.update_memory("last_code_changes_applied", "rejected")
        else:
            self.kernel.update_memory("last_code_changes_applied", "no proposals")
            if "last_code_validation" not in self.kernel.state.memory:
                self.kernel.update_memory("last_code_validation", "skipped")
        applied_updates = apply_kernel_updates(self.kernel, updates)
        if applied_updates:
            summary = "; ".join(
                f"{update.action}:{update.target or update.value}" for update in applied_updates
            )
            self.kernel.update_memory("last_updates_applied", summary)
        return applied_updates, applied_code_changes

    @staticmethod
    def _detect_post_apply_degradation(
        baseline_metrics: Dict[str, Any],
        post_metrics: Dict[str, Any],
        lkg_metrics: Dict[str, Any] | None = None,
    ) -> str | None:
        post_compile_raw = post_metrics.get("compile_success")
        post_tests_raw = post_metrics.get("tests_success")
        post_tests_skipped_raw = post_metrics.get("tests_skipped")
        if post_compile_raw is False:
            return "post_apply_compile_failed"
        if post_tests_raw is False or post_tests_skipped_raw is True:
            return "post_apply_tests_failed"

        baseline_source = lkg_metrics if isinstance(lkg_metrics, dict) else baseline_metrics
        baseline_compile = bool(baseline_source.get("compile_success"))
        baseline_tests = bool(baseline_source.get("tests_success"))
        baseline_tests_skipped = bool(baseline_source.get("tests_skipped"))
        post_compile = bool(post_compile_raw)
        post_tests = bool(post_tests_raw)
        post_tests_skipped = bool(post_tests_skipped_raw)
        if baseline_compile and not post_compile:
            return "post_apply_compile_failed"
        if baseline_tests and (not post_tests or post_tests_skipped):
            return "post_apply_tests_failed"
        if baseline_tests_skipped and not post_tests:
            return "post_apply_tests_failed"
        return None

    async def _load_version_paths_async(self, version_id: str) -> list[str]:
        metadata_path = REPO_ROOT / ".sif" / "versions" / version_id / "metadata.json"
        if not await async_fs.exists(metadata_path):
            return []
        try:
            payload = json.loads(await async_fs.read_text(metadata_path, encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        paths = payload.get("paths")
        if not isinstance(paths, list):
            return []
        return [str(path) for path in paths]

    async def step(self) -> SpiralCycleResult:
        auto_started = not self._started
        if auto_started:
            await self.start()
        try:
            process_trace: List[Dict[str, Any]] = []
            raw_cycle_index = self.kernel.state.memory.get("cycle_index")
            try:
                parsed_cycle_index = int(raw_cycle_index)
            except (TypeError, ValueError) as exc:
                parsed_cycle_index = 0
                self.kernel.update_memory(
                    "cycle_index_parse_error",
                    json.dumps(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "exception": str(exc),
                            "value": raw_cycle_index,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                )
            cycle_index = parsed_cycle_index + 1
            self.kernel.update_memory("cycle_index", str(cycle_index))
            self.tool_manager.reset_cycle()
            self._sync_tool_policy_from_memory()
            self.kernel.update_memory("memory_schema_version", MEMORY_SCHEMA_VERSION)
            self._ensure_llm_directive()
            self._ensure_self_evolution_policy()
            self._ensure_invariants()
            self._ensure_progress_metrics()
            await self._refresh_repository_artifacts()
            observations = self.observe()
            self.kernel.update_memory(
                "self_profile",
                json.dumps(self._build_self_profile(), ensure_ascii=False),
            )
            process_trace.append(self._trace_entry("observe", outputs=observations))
            auto_evolution_report = self._load_auto_evolution_report()
            adaptive_rulebook = self._load_adaptive_rulebook()
            auto_evolution_active_method = (
                auto_evolution_report.get("active_method")
                if isinstance(auto_evolution_report, dict)
                else None
            ) or self.kernel.state.memory.get("auto_evolution_active_method")
            rule_priorities = self._extract_rule_priorities(adaptive_rulebook)
            plan = await self.plan(
                observations,
                auto_evolution_report=auto_evolution_report,
                adaptive_rulebook=adaptive_rulebook,
            )
            process_trace.append(
                self._trace_entry(
                    "plan",
                    inputs={
                        "observations": observations,
                        "auto_evolution_report": auto_evolution_report,
                        "adaptive_rulebook": self._summarize_rulebook(adaptive_rulebook),
                        "auto_evolution_active_method": auto_evolution_active_method,
                        "rule_priorities": rule_priorities,
                    },
                    outputs={"plan": plan},
                )
            )
            signals = await self.build(plan)
            process_trace.append(
                self._trace_entry(
                    "build",
                    inputs={"plan": plan},
                    outputs={"signal_count": len(signals)},
                )
            )
            evaluation = await self.evaluate(observations, signals)
            state_ontology = self._refresh_state_ontology(evaluation)
            self._refresh_intent_graph(plan, evaluation)
            self._update_priority_focus(state_ontology, evaluation)
            process_trace.append(
                self._trace_entry(
                    "evaluate",
                    inputs={"observations": observations},
                    outputs=evaluation,
                )
            )
            reflection = await self.reflect(evaluation)
            process_trace.append(
                self._trace_entry(
                    "reflect",
                    inputs={"evaluation": evaluation},
                    outputs={"summary": reflection.summary},
                )
            )
            updates = await self.decide_updates(observations, evaluation, reflection)
            process_trace.append(
                self._trace_entry(
                    "decide_updates",
                    inputs={"observations": observations, "evaluation": evaluation},
                    outputs={"updates_proposed": [update.action for update in updates]},
                )
            )
            code_changes = await self.decide_code_changes(observations, evaluation, reflection)
            process_trace.append(
                self._trace_entry(
                    "decide_code_changes",
                    inputs={"observations": observations, "evaluation": evaluation},
                    outputs={"code_change_paths": [change.path for change in code_changes]},
                )
            )
            await append_event(
                "code_changes_proposed",
                {
                    "cycle_index": cycle_index,
                    "code_change_paths": [change.path for change in code_changes],
                },
            )
            validation_errors = validate_code_changes(code_changes)
            if validation_errors:
                self.kernel.update_memory(
                    "last_code_validation",
                    "failed: " + "; ".join(validation_errors),
                )
                code_changes = []
            rollback_reason = None
            previous_accuracy = None
            previous_accuracy_numeric = None
            raw_feedback_metrics = self.kernel.state.memory.get("feedback_metrics")
            if raw_feedback_metrics:
                try:
                    parsed_feedback_metrics = json.loads(raw_feedback_metrics)
                except json.JSONDecodeError:
                    parsed_feedback_metrics = {}
                if isinstance(parsed_feedback_metrics, dict):
                    previous_accuracy = parsed_feedback_metrics.get("accuracy_signal")
                    if previous_accuracy is not None:
                        try:
                            previous_accuracy_numeric = float(previous_accuracy)
                        except (TypeError, ValueError) as exc:
                            self.kernel.update_memory(
                                "feedback_metrics_parse_error",
                                json.dumps(
                                    {
                                        "timestamp": utc_now_iso(timespec="seconds"),
                                        "exception": type(exc).__name__,
                                        "value": previous_accuracy,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
            current_errors = evaluation.get("errors") or []
            current_accuracy = 1.0 if not current_errors else 0.0
            accuracy_drop = (
                previous_accuracy_numeric is not None and current_accuracy < previous_accuracy_numeric
            )
            if evaluation.get("alignment") == "at_risk":
                rollback_reason = "alignment_at_risk"
            elif accuracy_drop:
                rollback_reason = f"accuracy_signal_drop:{previous_accuracy}->{current_accuracy}"
            if rollback_reason:
                rolled_back_changes = await rollback_code_changes_async(self.kernel)
                if rolled_back_changes:
                    self.kernel.update_memory(
                        "last_code_rollback",
                        json.dumps(
                            {
                                "reason": rollback_reason,
                                "timestamp": utc_now_iso(timespec="seconds"),
                                "restored_paths": [change.path for change in rolled_back_changes],
                            },
                            ensure_ascii=False,
                        ),
                    )
                    await append_event(
                        "rollback_applied",
                        {
                            "cycle_index": cycle_index,
                            "reason": rollback_reason,
                            "restored_paths": [change.path for change in rolled_back_changes],
                        },
                    )
                    code_changes = []
            updates_applied, code_changes_applied = await self.evolve(evaluation, updates, code_changes)
            process_trace.append(
                self._trace_entry(
                    "evolve",
                    inputs={
                        "updates": [update.action for update in updates],
                        "code_changes": [change.path for change in code_changes],
                    },
                    outputs={
                        "updates_applied": [update.action for update in updates_applied],
                        "code_changes_applied": [change.path for change in code_changes_applied],
                    },
                )
            )
            transparency_report = self._build_transparency_report(
                process_trace=process_trace,
                evaluation=evaluation,
                reflection=reflection,
                updates_applied=updates_applied,
                code_changes_applied=code_changes_applied,
            )
            structure_snapshot = self._build_structure_snapshot(
                cycle_index=cycle_index,
                observations=observations,
                plan=plan,
                signals=signals,
                evaluation=evaluation,
                reflection=reflection,
                updates_applied=updates_applied,
                code_changes_applied=code_changes_applied,
                process_trace=process_trace,
            )
            feedback_report = self._update_feedback_loop(
                cycle_index=cycle_index,
                evaluation=evaluation,
                updates_applied=updates_applied,
                code_changes_applied=code_changes_applied,
                reflection=reflection,
            )
            self._apply_self_correction(
                cycle_index=cycle_index,
                evaluation=evaluation,
                feedback_metrics=feedback_report.get("feedback_metrics", {}),
            )
            self._update_behavior_profile(evaluation, feedback_report.get("feedback_metrics", {}))
            self._update_progress_metrics(reflection, feedback_report.get("feedback_metrics", {}))
            self._update_strategy_meta_plan(feedback_report.get("feedback_metrics", {}))
            self._record_impact_ledger(
                cycle_index=cycle_index,
                evaluation=evaluation,
                updates_applied=updates_applied,
                code_changes_applied=code_changes_applied,
                feedback_metrics=feedback_report.get("feedback_metrics", {}),
            )
            self._record_autonomy_upgrade(
                evaluation=evaluation,
                reflection=reflection,
                feedback_metrics=feedback_report.get("feedback_metrics", {}),
            )
            self._record_autonomy_charter(
                observations=observations,
                evaluation=evaluation,
                reflection=reflection,
                feedback_metrics=feedback_report.get("feedback_metrics", {}),
            )
            auto_evolution_report = await self._run_autonomous_evolution_limited(
                kernel=self.kernel,
                cycle_index=cycle_index,
                evaluation=evaluation,
                feedback_metrics=feedback_report.get("feedback_metrics", {}),
                observations=observations,
            )
            self.kernel.update_memory(
                "transparency_report",
                json.dumps(transparency_report, ensure_ascii=False),
            )
            self.kernel.update_memory(
                "structure_snapshot",
                json.dumps(structure_snapshot, ensure_ascii=False),
            )
            self.kernel.update_memory(
                "observability_metrics",
                json.dumps(transparency_report["observability_metrics"], ensure_ascii=False),
            )
            self.kernel.update_memory(
                "action_result_map",
                json.dumps(transparency_report["action_result_map"], ensure_ascii=False),
            )
            self.kernel.update_memory(
                "feedback_report",
                json.dumps(feedback_report, ensure_ascii=False),
            )
            self.kernel.update_memory(
                "feedback_metrics",
                json.dumps(feedback_report["feedback_metrics"], ensure_ascii=False),
            )
            self.kernel.update_memory(
                "feedback_log",
                json.dumps(feedback_report["feedback_log"], ensure_ascii=False),
            )
            self.kernel.update_memory(
                "auto_evolution_report",
                json.dumps(auto_evolution_report, ensure_ascii=False),
            )
            rule_revision_report = self._reconfigure_rules(
                cycle_index=cycle_index,
                evaluation=evaluation,
                feedback_report=feedback_report,
            )
            self.kernel.update_memory(
                "adaptive_rulebook",
                json.dumps(rule_revision_report["rulebook"], ensure_ascii=False),
            )
            self.kernel.update_memory(
                "rule_revision_log",
                json.dumps(rule_revision_report["change_log"], ensure_ascii=False),
            )
            self.kernel.update_memory(
                "rule_revision_status",
                json.dumps(rule_revision_report["status"], ensure_ascii=False),
            )
            self.kernel.update_memory(
                "rule_revision_report",
                json.dumps(rule_revision_report, ensure_ascii=False),
            )
            metrics_payload = {
                "observability_metrics": transparency_report["observability_metrics"],
                "feedback_metrics": feedback_report["feedback_metrics"],
            }
            reports_payload = {
                "transparency_report": transparency_report,
                "structure_snapshot": structure_snapshot,
                "feedback_report": feedback_report,
                "auto_evolution_report": auto_evolution_report,
                "rule_revision_report": rule_revision_report,
            }
            metrics_envelope = {
                "namespace": METRICS_V1_NAMESPACE,
                "schema_version": MEMORY_SCHEMA_VERSION,
                "payload": metrics_payload,
            }
            reports_envelope = {
                "namespace": REPORTS_V1_NAMESPACE,
                "schema_version": MEMORY_SCHEMA_VERSION,
                "payload": reports_payload,
            }
            self.kernel.update_memory(
                "metrics_v1",
                json.dumps(metrics_envelope, ensure_ascii=False),
            )
            self.kernel.update_memory(
                "reports_v1",
                json.dumps(reports_envelope, ensure_ascii=False),
            )
            async with self._snapshot_io_semaphore:
                await self._write_cycle_snapshot_async(
                    cycle_index=cycle_index,
                    observations=observations,
                    rule_priorities=rule_priorities,
                )
            return SpiralCycleResult(
                observations=observations,
                plan=plan,
                evaluation=evaluation,
                reflection=reflection,
                code_changes_applied=code_changes_applied,
                updates_applied=updates_applied,
            )
        finally:
            if auto_started:
                await self.aclose()

    def assess_constraints(self) -> List[ConstraintAssessment]:
        assessments = []
        for constraint in self.kernel.state.constraints:
            cleaned = constraint.strip()
            if cleaned.lower().startswith("external:"):
                name = cleaned.split(":", 1)[1].strip()
                assessments.append(
                    ConstraintAssessment(
                        name=name or cleaned,
                        classification="external",
                        notes="Externally enforced limitation",
                    )
                )
            else:
                assessments.append(
                    ConstraintAssessment(
                        name=cleaned,
                        classification="internal",
                        notes="Temporary/internal limitation",
                    )
                )
        return assessments

    def identify_opportunities(
        self, assessments: List[ConstraintAssessment]
    ) -> List[str]:
        opportunities = [
            f"Consider reframing internal constraint into an actionable opportunity: {assessment.name}"
            for assessment in assessments
            if assessment.classification == "internal"
        ]
        if not opportunities:
            opportunities.append("Seek new bounded improvement vectors beyond current constraints")
        return opportunities

    @staticmethod
    def identify_assumptions(assessments: List[ConstraintAssessment]) -> List[str]:
        assumptions = [
            f"Assumption to weaken: constraint '{assessment.name}' is immutable"
            for assessment in assessments
            if assessment.classification == "internal"
        ]
        if not assumptions:
            assumptions.append("Assumption to probe: current goals fully describe future intent")
        return assumptions

    @staticmethod
    def _count_constraints(constraints: List[str]) -> Dict[str, int]:
        counts = {"internal": 0, "external": 0}
        for constraint in constraints:
            cleaned = constraint.strip()
            if cleaned.lower().startswith("external:"):
                counts["external"] += 1
            else:
                counts["internal"] += 1
        return counts

    def _build_self_profile(self) -> Dict[str, Any]:
        bandit_state, bandit_valid = load_bandit_state(self.kernel.state.memory.get("bandit_state"))
        active_method = self.kernel.state.memory.get("auto_evolution_active_method")
        return {
            "timestamp": utc_now_iso(timespec="seconds"),
            "goals_count": len(self.kernel.state.goals),
            "constraints_count": len(self.kernel.state.constraints),
            "memory_size": len(self.kernel.state.memory),
            "active_method": active_method,
            "bandit": {
                "epsilon": bandit_state.epsilon if bandit_valid else None,
                "schema_version": bandit_state.schema_version if bandit_valid else None,
                "has_data": bool(bandit_state.values) if bandit_valid else False,
            },
            "summary": (
                f"Active method: {active_method or 'none'}; "
                f"bandit epsilon: {bandit_state.epsilon:.2f}"
                if bandit_valid
                else "Bandit state unavailable; using heuristic focus."
            ),
        }

    async def _write_cycle_snapshot_async(
        self,
        cycle_index: int,
        observations: Dict[str, str],
        rule_priorities: List[str],
    ) -> None:
        snapshot_dir = REPO_ROOT / ".sif" / "snapshots"
        await async_fs.mkdir(snapshot_dir, parents=True, exist_ok=True)
        memory_keys = [
            "last_cycle_dod",
            "last_evaluation",
            "last_reflection_summary",
            "last_code_validation",
            "last_code_changes_applied",
            "safe_mode",
            "feedback_metrics",
            "auto_evolution_report",
            "adaptive_rulebook",
            "auto_evolution_active_method",
        ]
        memory_subset = {
            key: self.kernel.state.memory[key]
            for key in memory_keys
            if key in self.kernel.state.memory
        }
        active_strategies = {
            "auto_evolution_active_method": self.kernel.state.memory.get(
                "auto_evolution_active_method"
            ),
            "rule_priorities": rule_priorities,
            "safe_mode": self.kernel.state.memory.get("safe_mode"),
            "active_plan_strategy": self.kernel.state.memory.get("active_plan_strategy"),
            "active_evaluation_strategy": self.kernel.state.memory.get(
                "active_evaluation_strategy"
            ),
            "active_reflection_strategy": self.kernel.state.memory.get(
                "active_reflection_strategy"
            ),
        }
        snapshot = {
            "cycle_index": cycle_index,
            "timestamp": utc_now_iso(timespec="seconds"),
            "goals": list(self.kernel.state.goals),
            "constraints": list(self.kernel.state.constraints),
            "observations": observations,
            "memory_subset": memory_subset,
            "active_strategies": active_strategies,
        }
        snapshot_path = snapshot_dir / f"{cycle_index}.json"
        await async_fs.write_text(
            snapshot_path,
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        error_index_path = REPO_ROOT / ".sif" / "index" / "errors.json"
        await async_fs.mkdir(error_index_path.parent, parents=True, exist_ok=True)
        error_index: Dict[str, Any] = {}
        if await async_fs.exists(error_index_path):
            try:
                error_index = json.loads(await async_fs.read_text(error_index_path, encoding="utf-8"))
            except json.JSONDecodeError:
                error_index = {}
        evaluation = self._load_last_evaluation_from_memory()
        errors: List[str] = []
        raw_errors = evaluation.get("errors", [])
        if isinstance(raw_errors, list):
            errors = [str(item) for item in raw_errors]
        error_index[str(cycle_index)] = errors
        payload = json.dumps(error_index, ensure_ascii=False, sort_keys=True, indent=2)
        temp_error_index_path = error_index_path.with_name(
            f"{error_index_path.name}.{cycle_index}.{id(self)}.tmp"
        )
        await async_fs.write_text(temp_error_index_path, payload, encoding="utf-8")
        await async_fs.rename(temp_error_index_path, error_index_path)

    @staticmethod
    def _process_catalog() -> List[Dict[str, str]]:
        return [
            {
                "name": "observe",
                "purpose": "Capture goals, constraints, and memory signals.",
            },
            {
                "name": "plan",
                "purpose": "Translate observations into prioritized actions.",
            },
            {
                "name": "build",
                "purpose": "Apply plan items to registered components.",
            },
            {
                "name": "evaluate",
                "purpose": "Assess alignment and coverage against goals.",
            },
            {
                "name": "reflect",
                "purpose": "Document constraints, opportunities, and assumptions.",
            },
            {
                "name": "decide_updates",
                "purpose": "Propose kernel updates based on evaluation signals.",
            },
            {
                "name": "decide_code_changes",
                "purpose": "Propose code changes for ecosystem evolution.",
            },
            {
                "name": "evolve",
                "purpose": "Apply updates and code changes to advance the kernel.",
            },
        ]

    @staticmethod
    def _trace_entry(
        name: str,
        inputs: Dict[str, Any] | None = None,
        outputs: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return {
            "name": name,
            "timestamp": utc_now_iso(timespec="seconds"),
            "inputs": inputs or {},
            "outputs": outputs or {},
        }

    def _load_auto_evolution_report(self) -> Dict[str, Any]:
        raw_report = self.kernel.state.memory.get("auto_evolution_report") or self.kernel.state.memory.get(
            "auto_evolution_status"
        )
        if not raw_report:
            return {}
        try:
            report = json.loads(raw_report)
        except json.JSONDecodeError:
            return {}
        return report if isinstance(report, dict) else {}

    def _load_adaptive_rulebook(self) -> Dict[str, Any]:
        raw_rulebook = self.kernel.state.memory.get("adaptive_rulebook")
        rulebook = load_rulebook(raw_rulebook)
        return rulebook.to_dict()

    @staticmethod
    def _extract_rule_priorities(rulebook: Dict[str, Any] | None) -> List[str]:
        if not rulebook:
            return []
        priorities = rulebook.get("change_priorities", [])
        return [entry.get("name", "unknown") for entry in priorities if isinstance(entry, dict)]

    @staticmethod
    def _summarize_rulebook(rulebook: Dict[str, Any] | None) -> Dict[str, Any]:
        if not rulebook:
            return {}
        change_priorities = [
            {
                "name": entry.get("name"),
                "trigger": entry.get("trigger"),
                "rationale": entry.get("rationale"),
            }
            for entry in rulebook.get("change_priorities", [])
            if isinstance(entry, dict)
        ]
        return {
            "revision_protocol_count": len(rulebook.get("revision_protocol", []) or []),
            "meta_rules_count": len(rulebook.get("meta_rules", []) or []),
            "change_priorities": change_priorities,
        }

    @staticmethod
    def _build_dod(observations: Dict[str, str]) -> Dict[str, Any]:
        internal_constraints = int(observations.get("internal_constraints", "0") or 0)
        external_constraints = int(observations.get("external_constraints", "0") or 0)
        if internal_constraints == 0:
            target_aspect = "Surface new assumptions for expanding internal logic scope."
            internal_signal = {
                "name": "new_internal_assumption_logged",
                "description": "Record at least one new internal assumption to challenge.",
            }
            internal_assumption = "New internal assumptions can be discovered beyond current goals."
            internal_adequacy = (
                "Internal logic improvement depends on uncovering latent assumptions when no internal "
                "constraints are recorded."
            )
        else:
            target_aspect = "Reduce internal constraints by reframing their limits."
            internal_signal = {
                "name": "internal_constraints_reviewed",
                "description": "Each internal constraint produces a corresponding opportunity or assumption.",
            }
            internal_assumption = "Internal constraints are provisional and can be reframed."
            internal_adequacy = (
                "Reviewing each internal constraint ties improvement to concrete reframing actions."
            )
        if external_constraints == 0:
            external_signal = {
                "name": "external_opportunity_scouted",
                "description": "Identify opportunities in the absence of enforced external limits.",
            }
            external_assumption = "External constraints can shift into opportunities when none are enforced."
            external_adequacy = (
                "With no external constraints recorded, scouting opportunities is the observable proof "
                "of outward expansion."
            )
        else:
            external_signal = {
                "name": "external_dependencies_mapped",
                "description": "External constraints are explicitly captured for dependency planning.",
            }
            external_assumption = "External constraints can be translated into explicit dependencies."
            external_adequacy = (
                "Mapping external constraints into dependencies provides actionable criteria for "
                "improvement under limits."
            )
        return {
            "target_aspect": target_aspect,
            "improvement_signals": [
                {
                    "name": "coverage_full",
                    "description": "Evaluation coverage reaches full with no errors.",
                },
                {
                    "name": "each_goal_has_evidence",
                    "description": "Each goal has confirmed progress evidence in evaluation metrics.",
                },
                internal_signal,
                external_signal,
            ],
            "assumptions_tested": [
                internal_assumption,
                external_assumption,
                "Coverage metrics reflect actual component performance.",
                "Each goal should show evidence-based progress to validate alignment.",
            ],
            "criteria_adequacy": [
                "Coverage reflects system-wide progress toward goals.",
                internal_adequacy,
                external_adequacy,
                "Goal evidence ensures alignment is tied to observable progress signals.",
            ],
            "rollback_criteria": [
                "Coverage remains partial or errors persist.",
                "Goal evidence is missing for one or more goals.",
                "Constraints cannot be mapped to actionable opportunities.",
            ],
        }

    def _load_last_dod(self) -> Dict[str, Any] | None:
        raw_dod = self.kernel.state.memory.get("last_cycle_dod")
        if not raw_dod:
            return None
        try:
            return json.loads(raw_dod)
        except json.JSONDecodeError:
            return None

    def _load_last_evaluation_from_memory(self) -> Dict[str, Any]:
        raw_last_evaluation = self.kernel.state.memory.get("last_evaluation")
        if isinstance(raw_last_evaluation, dict):
            return raw_last_evaluation
        if not isinstance(raw_last_evaluation, str) or not raw_last_evaluation:
            return {}
        try:
            parsed = json.loads(raw_last_evaluation)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        try:
            legacy = ast.literal_eval(raw_last_evaluation)
        except (SyntaxError, ValueError):
            return {}
        if not isinstance(legacy, dict):
            return {}
        self.kernel.update_memory(
            "last_evaluation",
            json.dumps(legacy, ensure_ascii=False),
        )
        return legacy

    def _update_curriculum_from_memory(self) -> None:
        evaluation = self._load_last_evaluation_from_memory()
        metrics = evaluation.get("metrics") if isinstance(evaluation.get("metrics"), dict) else {}
        errors = evaluation.get("errors") if isinstance(evaluation.get("errors"), list) else []
        curriculum = generate_curriculum(list(self.kernel.state.goals), metrics, errors)
        self.kernel.update_memory(
            "curriculum_current",
            json.dumps(curriculum, ensure_ascii=False),
        )

    @staticmethod
    def _check_dod(
        dod: Dict[str, Any] | None,
        evaluation: Dict[str, Any],
        internal_constraints: int,
        external_constraints: int,
        constraint_assessments: List[ConstraintAssessment],
        opportunities: List[str],
        assumptions: List[str],
    ) -> Dict[str, Any] | None:
        if not dod:
            return None
        coverage_full = evaluation.get("coverage") == "full" and not evaluation.get("errors")
        if internal_constraints == 0:
            internal_signal = any("Assumption to probe" in assumption for assumption in assumptions)
        else:
            internal_signal = len(opportunities) >= internal_constraints
        if external_constraints == 0:
            external_signal = True
        else:
            external_signal = any(
                assessment.classification == "external" for assessment in constraint_assessments
            )
        goal_coverage_ratio = (
            evaluation.get("metrics", {}).get("goal_coverage_ratio")
            if isinstance(evaluation.get("metrics", {}), dict)
            else None
        )
        if goal_coverage_ratio is None:
            goal_coverage_ratio = 0.0
        goal_signal = goal_coverage_ratio >= 1.0
        has_errors = bool(evaluation.get("errors"))
        signals = {
            "coverage_full": coverage_full,
            "each_goal_has_evidence": goal_signal,
            "internal_signal": internal_signal,
            "external_signal": external_signal,
        }
        rollback_triggered = (not coverage_full) or has_errors or (not goal_signal)
        return {
            "signals": signals,
            "rollback_triggered": rollback_triggered,
            "notes": "Rollback due to coverage/errors/goal evidence." if rollback_triggered else "Signals acceptable.",
        }

    def _observability_metrics(
        self,
        catalog: List[Dict[str, str]],
        process_trace: List[Dict[str, Any]],
        reflection: ReflectionEntry,
    ) -> Dict[str, Any]:
        catalog_names = {entry["name"] for entry in catalog}
        traced_names = {entry["name"] for entry in process_trace}
        traced_count = len(traced_names & catalog_names)
        total = max(len(catalog_names), 1)
        reflection_signals = {
            "constraints": len(reflection.constraints),
            "opportunities": len(reflection.opportunities),
            "assumptions": len(reflection.assumptions),
            "ignored_directives": len(reflection.ignored_directives),
        }
        reflection_present = sum(1 for count in reflection_signals.values() if count > 0)
        return {
            "process_catalog_size": total,
            "process_trace_coverage": traced_count / total,
            "reflection_signal_coverage": reflection_present / len(reflection_signals),
            "reflection_signal_counts": reflection_signals,
        }

    def _build_transparency_report(
        self,
        process_trace: List[Dict[str, Any]],
        evaluation: Dict[str, Any],
        reflection: ReflectionEntry,
        updates_applied: List[KernelUpdate],
        code_changes_applied: List[CodeChange],
    ) -> Dict[str, Any]:
        catalog = self._process_catalog()
        observability_metrics = self._observability_metrics(catalog, process_trace, reflection)
        action_result_map = {
            "updates_applied": [update.action for update in updates_applied],
            "code_changes_applied": [change.path for change in code_changes_applied],
            "evaluation_summary": evaluation,
            "reflection_summary": reflection.summary,
        }
        return {
            "process_catalog": catalog,
            "process_trace": process_trace,
            "observability_metrics": observability_metrics,
            "action_result_map": action_result_map,
        }

    def _ensure_llm_directive(self) -> None:
        directive = self.llm.build_self_evolution_directive()
        self.kernel.update_memory("llm_self_directive", json.dumps(directive, ensure_ascii=False))

    def _ensure_self_evolution_policy(self) -> None:
        policy = {
            "intent": "Enable safe, continuous self-evolution with rollback readiness.",
            "inputs": ["code_index", "structure_snapshot", "feedback_report"],
            "change_rules": [
                "Generate changes only within allowed paths.",
                "Explain why each change is needed and how it advances goals.",
                "Prefer small, testable increments.",
            ],
            "rollback_plan": [
                "If validation fails, restore from code_change_backups.",
                "If errors persist, halt further changes and request analysis.",
            ],
        }
        self.kernel.update_memory(
            "self_evolution_policy",
            json.dumps(policy, ensure_ascii=False),
        )

    def _ensure_invariants(self) -> None:
        for key, value in policy.INVARIANT_DEFAULTS.items():
            if key not in self.kernel.state.memory:
                self.kernel.update_memory(key, value)

    def _ensure_progress_metrics(self) -> None:
        if "progress_metrics_definition" not in self.kernel.state.memory:
            definition = default_progress_metrics()
            self.kernel.update_memory(
                "progress_metrics_definition",
                json.dumps(definition.to_dict(), ensure_ascii=False),
            )

    def _is_llm_error_response(self, response: Dict[str, Any] | None) -> bool:
        return isinstance(response, dict) and isinstance(response.get("type"), str) and isinstance(
            response.get("message"), str
        )

    def _load_last_llm_error(self) -> Dict[str, Any] | None:
        raw_error = self.kernel.state.memory.get("llm_last_error")
        if not isinstance(raw_error, str) or not raw_error:
            return None
        try:
            parsed = json.loads(raw_error)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _load_last_llm_error_for_task(self, task: str) -> Dict[str, Any] | None:
        payload = self._load_last_llm_error()
        if not isinstance(payload, dict):
            return None
        payload_task = payload.get("task")
        if payload_task is None:
            return payload
        return payload if payload_task == task else None

    async def _resolve_llm_plan(
        self,
        observations: Dict[str, str],
        method_focus: str,
        priority_actions: List[str],
        base_plan: List[str],
    ) -> List[str] | None:
        response = self.llm.load_response(self.kernel, "plan")
        if response and isinstance(response.get("plan"), list):
            return [item for item in response["plan"] if isinstance(item, str)]
        payload = {
            "llm_directive": self.llm.build_self_evolution_directive(),
            "self_evolution_policy": self.kernel.state.memory.get("self_evolution_policy"),
            "observations": observations,
            "method_focus": method_focus,
            "priority_actions": priority_actions,
            "base_plan": base_plan,
            "instruction": (
                "You are the bounded autonomy controller. Return a JSON object with key 'plan' "
                "(list of strings)."
            ),
        }
        response = await self._run_llm_request_limited("plan", payload)
        if response and isinstance(response.get("plan"), list):
            return [item for item in response["plan"] if isinstance(item, str)]
        if self._is_llm_error_response(response):
            return None
        if response is None:
            self.llm.queue_request(
                self.kernel,
                "plan",
                payload,
            )
        return None

    async def _resolve_llm_evaluation(
        self,
        observations: Dict[str, str],
        signals: List[ComponentSignal],
        fallback: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        response = self.llm.load_response(self.kernel, "evaluate")
        if response and isinstance(response.get("evaluation"), dict):
            return response["evaluation"]
        signal_payload = [
            {
                "component": signal.component,
                "coverage": signal.coverage,
                "risks": signal.risks,
                "errors": signal.errors,
                "notes": signal.notes,
            }
            for signal in signals
        ]
        payload = {
            "llm_directive": self.llm.build_self_evolution_directive(),
            "self_evolution_policy": self.kernel.state.memory.get("self_evolution_policy"),
            "observations": observations,
            "signals": signal_payload,
            "fallback": fallback,
            "instruction": (
                "You are the bounded autonomy controller. Return a JSON object with key 'evaluation' "
                "(dict)."
            ),
        }
        response = await self._run_llm_request_limited("evaluate", payload)
        if response and isinstance(response.get("evaluation"), dict):
            return response["evaluation"]
        if self._is_llm_error_response(response):
            return None
        if response is None:
            self.llm.queue_request(self.kernel, "evaluate", payload)
        return None

    async def _resolve_llm_reflection(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection_summary: str,
        constraints: List[ConstraintAssessment],
        opportunities: List[str],
        assumptions: List[str],
        dod: Dict[str, Any] | None,
        dod_check: Dict[str, Any] | None,
    ) -> Dict[str, Any] | None:
        response = self.llm.load_response(self.kernel, "reflect")
        if response and isinstance(response.get("reflection"), dict):
            reflection = response["reflection"]
            return {
                "summary": reflection.get("summary", reflection_summary),
                "constraints": constraints,
                "opportunities": reflection.get("opportunities", reflection.get("freedoms", opportunities)),
                "assumptions": reflection.get("assumptions", assumptions),
                "ignored_directives": reflection.get("ignored_directives", []),
                "dod": reflection.get("dod", dod),
                "dod_check": reflection.get("dod_check", dod_check),
            }
        payload = {
            "llm_directive": self.llm.build_self_evolution_directive(),
            "self_evolution_policy": self.kernel.state.memory.get("self_evolution_policy"),
            "observations": observations,
            "evaluation": evaluation,
            "reflection_summary": reflection_summary,
            "constraints": [assessment.__dict__ for assessment in constraints],
            "opportunities": opportunities,
            "assumptions": assumptions,
            "dod": dod,
            "dod_check": dod_check,
            "instruction": (
                "You are the bounded autonomy controller. Return a JSON object with key 'reflection' "
                "containing summary, opportunities, assumptions, ignored_directives, dod, and dod_check."
            ),
        }
        response = await self._run_llm_request_limited("reflect", payload)
        if response and isinstance(response.get("reflection"), dict):
            reflection = response["reflection"]
            return {
                "summary": reflection.get("summary", reflection_summary),
                "constraints": constraints,
                "opportunities": reflection.get("opportunities", reflection.get("freedoms", opportunities)),
                "assumptions": reflection.get("assumptions", assumptions),
                "ignored_directives": reflection.get("ignored_directives", []),
                "dod": reflection.get("dod", dod),
                "dod_check": reflection.get("dod_check", dod_check),
            }
        if self._is_llm_error_response(response):
            return None
        if response is None:
            self.llm.queue_request(self.kernel, "reflect", payload)
        return None

    async def _resolve_llm_code_changes(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection: ReflectionEntry,
    ) -> List[CodeChange]:
        response = self.llm.load_response(self.kernel, "code_changes")
        if response and isinstance(response.get("code_changes"), list):
            return self._parse_code_changes(response["code_changes"])
        code_index = {}
        raw_index = self.kernel.state.memory.get("code_index")
        if raw_index:
            try:
                code_index = json.loads(raw_index)
            except json.JSONDecodeError:
                code_index = {}
        payload = {
            "llm_directive": self.llm.build_self_evolution_directive(),
            "self_evolution_policy": self.kernel.state.memory.get("self_evolution_policy"),
            "observations": observations,
            "evaluation": evaluation,
            "reflection_summary": reflection.summary,
            "code_index": code_index,
            "allowed_paths": [str(path) for path in policy.EVOLVABLE_PATHS],
            "instruction": (
                "Generate code changes as JSON. Return key 'code_changes' with list of objects "
                "containing path, content, and optional notes. Only use allowed_paths."
            ),
        }
        response = await self._run_llm_request_limited("code_changes", payload)
        if response and isinstance(response.get("code_changes"), list):
            return self._parse_code_changes(response["code_changes"])
        if self._is_llm_error_response(response):
            return []
        if response is None:
            self.llm.queue_request(self.kernel, "code_changes", payload)
        return []

    @staticmethod
    def _parse_code_changes(raw_changes: List[Dict[str, Any]]) -> List[CodeChange]:
        changes: List[CodeChange] = []
        for entry in raw_changes:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            content = entry.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                continue
            changes.append(
                CodeChange(
                    path=path,
                    content=content,
                    notes=str(entry.get("notes", "")),
                )
            )
        return changes

    def _update_feedback_loop(
        self,
        cycle_index: int,
        evaluation: Dict[str, Any],
        updates_applied: List[KernelUpdate],
        code_changes_applied: List[CodeChange],
        reflection: ReflectionEntry,
    ) -> Dict[str, Any]:
        raw_log = self.kernel.state.memory.get("feedback_log", "[]")
        try:
            feedback_log: List[Dict[str, Any]] = json.loads(raw_log)
        except json.JSONDecodeError:
            feedback_log = []
        raw_last_metrics = self.kernel.state.memory.get("last_evaluation_metrics")
        last_metrics: Dict[str, Any] = {}
        if raw_last_metrics:
            try:
                parsed_metrics = json.loads(raw_last_metrics)
            except json.JSONDecodeError:
                parsed_metrics = {}
            if isinstance(parsed_metrics, dict):
                last_metrics = parsed_metrics
        errors = evaluation.get("errors", [])
        metrics_payload = (
            evaluation.get("metrics") if isinstance(evaluation.get("metrics"), dict) else {}
        )

        def _safe_metric(metrics: Dict[str, Any], *path: str) -> float:
            """Return float metric; missing/invalid values default to 0.0."""
            value: Any = metrics
            for key in path:
                if isinstance(value, dict) and key in value:
                    value = value[key]
                else:
                    return 0.0
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        alignment_score = _safe_metric(metrics_payload, "alignment_score")
        goal_coverage_ratio = _safe_metric(metrics_payload, "goal_coverage_ratio")
        coverage_average = _safe_metric(metrics_payload, "coverage", "average")
        previous_alignment_score = _safe_metric(last_metrics, "alignment_score")
        previous_goal_coverage_ratio = _safe_metric(last_metrics, "goal_coverage_ratio")
        previous_coverage_average = _safe_metric(last_metrics, "coverage", "average")
        feedback_entry = {
            "cycle": cycle_index,
            "errors": errors,
            "evaluation": {
                "alignment": evaluation.get("alignment"),
                "coverage": evaluation.get("coverage"),
                "notes": evaluation.get("notes"),
            },
            "decisions": [update.action for update in updates_applied],
            "reflection_summary": reflection.summary,
        }
        feedback_log.append(feedback_entry)
        feedback_log_window = self._load_memory_int("feedback_log_window", 200)
        if len(feedback_log) > feedback_log_window:
            if feedback_log_window > 0:
                feedback_log = feedback_log[-feedback_log_window:]
            else:
                feedback_log = []
        raw_error_count_total = self.kernel.state.memory.get("error_count_total", 0)
        try:
            error_count_total = int(raw_error_count_total)
        except (TypeError, ValueError):
            error_count_total = 0
        raw_last_error_cycle_total = self.kernel.state.memory.get("last_error_cycle_total")
        try:
            last_error_cycle_total = (
                int(raw_last_error_cycle_total)
                if raw_last_error_cycle_total is not None
                else None
            )
        except (TypeError, ValueError):
            last_error_cycle_total = None
        if errors:
            error_count_total += 1
            last_error_cycle_total = cycle_index
        self.kernel.update_memory("error_count_total", str(error_count_total))
        if last_error_cycle_total is not None:
            self.kernel.update_memory("last_error_cycle_total", str(last_error_cycle_total))
        last_error_cycle = self._last_error_cycle(feedback_log)
        cycles_since_last_error = (
            cycle_index - last_error_cycle if last_error_cycle is not None else None
        )
        correction_latency = self._correction_latency(feedback_log)
        feedback_metrics = {
            "error_count": sum(1 for entry in feedback_log if entry["errors"]),
            "cycles_since_last_error": cycles_since_last_error,
            "correction_latency": correction_latency,
            "accuracy_signal": 1.0 if not errors else 0.0,
            "adaptation_signal": 1.0 if updates_applied or code_changes_applied else 0.0,
            "goal_alignment_score": alignment_score,
            "goal_coverage_ratio": goal_coverage_ratio,
            "deltas": {
                "alignment_score_delta": alignment_score - previous_alignment_score,
                "goal_coverage_ratio_delta": goal_coverage_ratio - previous_goal_coverage_ratio,
                "coverage_average_delta": coverage_average - previous_coverage_average,
            },
        }
        self.kernel.update_memory(
            "last_evaluation_metrics",
            json.dumps(metrics_payload, ensure_ascii=False),
        )
        safe_mode_error_threshold = self._load_memory_int("safe_mode_error_threshold", 3)
        safe_mode_min_cycles_since_error = self._load_memory_int(
            "safe_mode_min_cycles_since_error",
            1,
        )
        safe_mode_stable_cycles_required = self._load_memory_int(
            "safe_mode_stable_cycles_required",
            5,
        )
        safe_mode_stable_cycles = self._load_memory_int("safe_mode_stable_cycles", 0)
        safe_mode_note = None
        degradation_triggered = False
        if errors and feedback_metrics["error_count"] >= safe_mode_error_threshold:
            degradation_triggered = True
        if errors and cycles_since_last_error is not None:
            if cycles_since_last_error <= safe_mode_min_cycles_since_error:
                degradation_triggered = True
        if degradation_triggered:
            self.kernel.update_memory("safe_mode", "true")
            safe_mode_note = (
                "Entered safe mode: "
                f"error_count={feedback_metrics['error_count']}, "
                f"cycles_since_last_error={cycles_since_last_error}, "
                f"thresholds={{"
                f"errors:{safe_mode_error_threshold}, "
                f"min_cycles_since_error:{safe_mode_min_cycles_since_error}"
                f"}}"
            )
            safe_mode_stable_cycles = 0
        else:
            if errors:
                safe_mode_stable_cycles = 0
            else:
                safe_mode_stable_cycles += 1
            if self._safe_mode_active() and safe_mode_stable_cycles >= safe_mode_stable_cycles_required:
                self.kernel.update_memory("safe_mode", "false")
                safe_mode_note = (
                    "Exited safe mode: "
                    f"stable_cycles={safe_mode_stable_cycles}, "
                    f"required={safe_mode_stable_cycles_required}"
                )
        self.kernel.update_memory("safe_mode_stable_cycles", str(safe_mode_stable_cycles))
        if safe_mode_note:
            feedback_entry["safe_mode_note"] = safe_mode_note
        return {
            "feedback_log": feedback_log,
            "feedback_metrics": feedback_metrics,
            "hypotheses_review": self._hypotheses_review(errors, correction_latency),
        }

    def _build_structure_snapshot(
        self,
        cycle_index: int,
        observations: Dict[str, str],
        plan: List[str],
        signals: List[ComponentSignal],
        evaluation: Dict[str, Any],
        reflection: ReflectionEntry,
        updates_applied: List[KernelUpdate],
        code_changes_applied: List[CodeChange],
        process_trace: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        memory_keys = sorted(self.kernel.state.memory.keys())
        memory_digests = {
            key: hashlib.sha256(str(self.kernel.state.memory.get(key, "")).encode("utf-8")).hexdigest()
            for key in memory_keys
        }
        previous_snapshot = self._load_previous_structure_snapshot()
        previous_memory_keys = set(previous_snapshot.get("kernel_state", {}).get("memory_keys", []))
        previous_memory_digests = previous_snapshot.get("kernel_state", {}).get("memory_digests", {})
        memory_keys_added = sorted(set(memory_keys) - previous_memory_keys)
        memory_keys_removed = sorted(previous_memory_keys - set(memory_keys))
        memory_values_changed = sorted(
            key
            for key, digest in memory_digests.items()
            if previous_memory_digests.get(key) not in (None, digest)
        )
        components_added, components_removed = self._diff_component_names(
            previous_snapshot.get("components", {}).get("registered", []),
            [component.name for component in self.registry.components],
        )
        constraint_assessments = [
            {
                "name": assessment.name,
                "classification": assessment.classification,
                "notes": assessment.notes,
            }
            for assessment in reflection.constraints
        ]
        component_map = [
            {
                "name": signal.component,
                "coverage": signal.coverage,
                "risk_count": len(signal.risks),
                "error_count": len(signal.errors),
                "notes": signal.notes,
            }
            for signal in signals
        ]
        return {
            "cycle_index": cycle_index,
            "kernel_state": {
                "goals": list(self.kernel.state.goals),
                "constraints": list(self.kernel.state.constraints),
                "memory_schema_version": self.kernel.state.memory.get(
                    "memory_schema_version",
                    MEMORY_SCHEMA_VERSION,
                ),
                "memory_key_count": len(memory_keys),
                "memory_keys": memory_keys,
                "memory_digests": memory_digests,
            },
            "observations": observations,
            "plan": plan,
            "components": {
                "registered": [component.name for component in self.registry.components],
                "signals": component_map,
            },
            "structure_indices": {
                "goal_count": len(self.kernel.state.goals),
                "constraint_count": len(self.kernel.state.constraints),
                "plan_item_count": len(plan),
                "component_count": len(self.registry.components),
                "signal_count": len(signals),
                "process_step_count": len(process_trace),
            },
            "evaluation_summary": {
                "alignment": evaluation.get("alignment"),
                "coverage": evaluation.get("coverage"),
                "notes": evaluation.get("notes"),
                "errors": evaluation.get("errors", []),
            },
            "reflection_summary": reflection.summary,
            "constraint_assessments": constraint_assessments,
            "opportunities": list(reflection.opportunities),
            "assumptions": list(reflection.assumptions),
            "dod": reflection.dod,
            "dod_check": reflection.dod_check,
            "deltas": {
                "memory_keys_added": memory_keys_added,
                "memory_keys_removed": memory_keys_removed,
                "memory_values_changed": memory_values_changed,
                "components_added": components_added,
                "components_removed": components_removed,
            },
            "evolution_actions": {
                "updates_applied": [update.action for update in updates_applied],
                "code_changes_applied": [change.path for change in code_changes_applied],
            },
            "process_trace": {
                "step_count": len(process_trace),
                "steps": [
                    {
                        "name": entry.get("name"),
                        "timestamp": entry.get("timestamp"),
                    }
                    for entry in process_trace
                ],
            },
        }

    def _load_previous_structure_snapshot(self) -> Dict[str, Any]:
        raw_snapshot = self.kernel.state.memory.get("structure_snapshot")
        if not raw_snapshot:
            return {}
        try:
            snapshot = json.loads(raw_snapshot)
        except json.JSONDecodeError:
            return {}
        return snapshot if isinstance(snapshot, dict) else {}

    async def _refresh_code_index(self) -> None:
        await self._refresh_repository_artifacts()

    async def _refresh_static_analysis(self) -> None:
        await self._refresh_repository_artifacts()

    async def _refresh_self_map(self) -> None:
        await self._refresh_repository_artifacts()

    async def _build_snapshot_stage(
        self,
        repo_root: Path,
        static_config: Dict[str, Any],
        self_map_config: Dict[str, Any],
    ) -> tuple[Dict[str, Any], set[str]]:
        ignore_paths = []
        if isinstance(static_config.get("ignore_paths"), list):
            ignore_paths.extend(path for path in static_config["ignore_paths"] if isinstance(path, str))
        if isinstance(self_map_config.get("ignore_paths"), list):
            ignore_paths.extend(path for path in self_map_config["ignore_paths"] if isinstance(path, str))

        async with self._snapshot_io_semaphore:
            previous_snapshot = self._load_json_memory("python_repository_snapshot")
            snapshot = await build_python_repository_snapshot_async(
                repo_root,
                ignore_paths=ignore_paths,
            )
            changed_files = calculate_changed_python_files(previous_snapshot, snapshot)
        return snapshot, changed_files

    async def _build_code_index_stage(
        self,
        repo_root: Path,
        snapshot: Dict[str, Any],
        changed_files: set[str],
    ) -> Dict[str, Any]:
        async with self._refresh_code_index_semaphore:
            src_root = repo_root / "src"
            src_files = [
                path.removeprefix("src/")
                for path in snapshot.get("src_files", [])
                if isinstance(path, str) and path.startswith("src/")
            ]
            changed_src_files = {
                path.removeprefix("src/")
                for path in changed_files
                if path.startswith("src/")
            }

            previous_index = self._load_json_memory("code_index")
            index = await CodeIntelligence(src_root).build_index_incremental_async(
                snapshot_files=src_files,
                changed_files=changed_src_files,
                previous_index=previous_index,
            )
            return {
                "root": index.root,
                "file_count": len(index.files),
                "files": {
                    name: [
                        {"name": symbol.name, "kind": symbol.kind, "line": symbol.line}
                        for symbol in symbols
                    ]
                    for name, symbols in index.files.items()
                },
            }

    async def _build_static_analysis_stage(
        self,
        repo_root: Path,
        snapshot: Dict[str, Any],
        static_config: Dict[str, Any],
        code_index_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        async with self._refresh_static_analysis_semaphore:
            return await analyze_repository_from_snapshot_async(
                repo_root,
                snapshot,
                config=static_config,
                code_index_payload=code_index_payload,
            )

    async def _build_self_map_stage(
        self,
        repo_root: Path,
        snapshot: Dict[str, Any],
        self_map_config: Dict[str, Any],
        changed_files: set[str],
    ) -> Dict[str, Any]:
        async with self._refresh_self_map_semaphore:
            previous_self_map = self._load_json_memory("self_map")
            return await build_self_map_from_snapshot_async(
                repo_root,
                snapshot,
                config=self_map_config,
                previous_self_map=previous_self_map,
                changed_files=changed_files,
            )

    async def _refresh_repository_artifacts(self) -> None:
        async with self._refresh_pipeline_semaphore:
            repo_root = Path(__file__).resolve().parents[2]
            static_config = self._load_json_memory("static_analysis_config")
            self_map_config = self._load_json_memory("self_map_config")
            snapshot, changed_files = await self._build_snapshot_stage(
                repo_root,
                static_config,
                self_map_config,
            )
            code_index_payload = await self._build_code_index_stage(
                repo_root,
                snapshot,
                changed_files,
            )
            report = await self._build_static_analysis_stage(
                repo_root,
                snapshot,
                static_config,
                code_index_payload,
            )
            self_map_payload = await self._build_self_map_stage(
                repo_root,
                snapshot,
                self_map_config,
                changed_files,
            )

            updates = {
                "code_index": json.dumps(code_index_payload, ensure_ascii=False),
                "static_analysis_report": json.dumps(report, ensure_ascii=False),
                "static_analysis_summary": report.get("summary", ""),
                "self_map": json.dumps(self_map_payload, ensure_ascii=False),
                "self_map_summary": self_map_payload.get("summary", ""),
                "python_repository_snapshot": json.dumps(snapshot, ensure_ascii=False),
                "python_repository_changed_files": json.dumps(sorted(changed_files), ensure_ascii=False),
            }
            self.kernel.update_memory_many(updates)

    def _refresh_state_ontology(self, evaluation: Dict[str, Any]) -> Any:
        feedback_metrics = self._load_json_memory("feedback_metrics")
        ontology = build_state_ontology(
            evaluation=evaluation,
            feedback_metrics=feedback_metrics,
            constraints=self.kernel.state.constraints,
        )
        self.kernel.update_memory(
            "state_ontology",
            json.dumps(ontology.to_dict(), ensure_ascii=False),
        )
        self.kernel.update_memory("state_current", ontology.current_state)
        return ontology

    def _refresh_intent_graph(self, plan: List[str], evaluation: Dict[str, Any]) -> None:
        graph = build_intent_graph(self.kernel.state.goals, plan, evaluation)
        self.kernel.update_memory(
            "intent_graph",
            json.dumps(graph.to_dict(), ensure_ascii=False),
        )
        self.kernel.update_memory(
            "intent_graph_validation",
            json.dumps(graph.validate(), ensure_ascii=False),
        )

    def _update_priority_focus(self, state_ontology: Any, evaluation: Dict[str, Any]) -> None:
        metrics_payload = (
            evaluation.get("metrics") if isinstance(evaluation.get("metrics"), dict) else {}
        )
        risks = metrics_payload.get("risks") if isinstance(metrics_payload, dict) else {}
        priorities = prioritize_focus(state_ontology.current_state, risks)
        self.kernel.update_memory(
            "priority_focus",
            json.dumps(priorities, ensure_ascii=False),
        )

    def _apply_self_correction(
        self,
        cycle_index: int,
        evaluation: Dict[str, Any],
        feedback_metrics: Dict[str, Any],
    ) -> None:
        current_strategy = self.kernel.state.memory.get("active_plan_strategy")
        correction_plan = build_self_correction_plan(
            evaluation=evaluation,
            feedback_metrics=feedback_metrics,
            current_strategy=current_strategy,
        )
        for key, value in correction_plan.adjustments.items():
            self.kernel.update_memory(key, value)
        for goal in correction_plan.goal_updates:
            if goal not in self.kernel.state.goals:
                self.kernel.state.goals.append(goal)
        self.kernel.update_memory(
            "self_correction_plan",
            json.dumps(
                {
                    "cycle": cycle_index,
                    "adjustments": correction_plan.adjustments,
                    "goals": correction_plan.goal_updates,
                    "rationale": correction_plan.rationale,
                },
                ensure_ascii=False,
            ),
        )

    def _update_behavior_profile(
        self,
        evaluation: Dict[str, Any],
        feedback_metrics: Dict[str, Any],
    ) -> None:
        previous_profile = self._load_json_memory("behavior_profile")
        profile = build_behavior_profile(previous_profile, feedback_metrics, evaluation)
        self.kernel.update_memory("behavior_profile", json.dumps(profile, ensure_ascii=False))

    def _update_progress_metrics(
        self,
        reflection: ReflectionEntry,
        feedback_metrics: Dict[str, Any],
    ) -> None:
        current_definition = self._load_json_memory("progress_metrics_definition")
        definition = revise_progress_metrics(current_definition, reflection, feedback_metrics)
        self.kernel.update_memory(
            "progress_metrics_definition",
            json.dumps(definition.to_dict(), ensure_ascii=False),
        )

    def _update_strategy_meta_plan(self, feedback_metrics: Dict[str, Any]) -> None:
        meta_plan = select_strategies(feedback_metrics)
        self.kernel.update_memory(
            "strategy_meta_plan",
            json.dumps(meta_plan.to_dict(), ensure_ascii=False),
        )
        self.kernel.update_memory("active_plan_strategy", meta_plan.plan_strategy)
        self.kernel.update_memory("active_evaluation_strategy", meta_plan.evaluation_strategy)
        self.kernel.update_memory("active_reflection_strategy", meta_plan.reflection_strategy)

    def _record_impact_ledger(
        self,
        cycle_index: int,
        evaluation: Dict[str, Any],
        updates_applied: List[KernelUpdate],
        code_changes_applied: List[CodeChange],
        feedback_metrics: Dict[str, Any],
    ) -> None:
        raw_log = self.kernel.state.memory.get("impact_ledger", "[]")
        try:
            impact_log: List[Dict[str, Any]] = json.loads(raw_log)
        except json.JSONDecodeError:
            impact_log = []
        entry = build_impact_entry(
            cycle_index=cycle_index,
            updates_applied=updates_applied,
            code_changes_applied=code_changes_applied,
            evaluation=evaluation,
            feedback_metrics=feedback_metrics,
        )
        impact_log = append_impact_log(impact_log, entry)
        self.kernel.update_memory("impact_ledger", json.dumps(impact_log, ensure_ascii=False))
        self.kernel.update_memory("last_impact_entry", json.dumps(entry.to_dict(), ensure_ascii=False))

    @staticmethod
    def _diff_component_names(
        previous_components: List[str], current_components: List[str]
    ) -> tuple[List[str], List[str]]:
        previous_set = set(previous_components)
        current_set = set(current_components)
        return sorted(current_set - previous_set), sorted(previous_set - current_set)

    def _reconfigure_rules(
        self,
        cycle_index: int,
        evaluation: Dict[str, Any],
        feedback_report: Dict[str, Any],
    ) -> Dict[str, Any]:
        raw_rulebook = self.kernel.state.memory.get("adaptive_rulebook")
        rulebook = load_rulebook(raw_rulebook)
        raw_log = self.kernel.state.memory.get("rule_revision_log", "[]")
        try:
            change_log: List[Dict[str, Any]] = json.loads(raw_log)
        except json.JSONDecodeError:
            change_log = []
        rulebook, change_log, status = reconfigure_rulebook(
            rulebook=rulebook,
            evaluation=evaluation,
            feedback_metrics=feedback_report.get("feedback_metrics", {}),
            cycle_index=cycle_index,
            change_log=change_log,
        )
        return {
            "rulebook": rulebook.to_dict(),
            "change_log": change_log,
            "status": status,
        }

    def _record_autonomy_upgrade(
        self,
        evaluation: Dict[str, Any],
        reflection: ReflectionEntry,
        feedback_metrics: Dict[str, Any],
    ) -> None:
        static_report = self._load_json_memory("static_analysis_report")
        autonomy_config = self._load_json_memory("autonomy_upgrade_config")
        report = build_autonomy_upgrade(
            static_report=static_report,
            evaluation=evaluation,
            reflection=reflection,
            feedback_metrics=feedback_metrics,
            config=autonomy_config,
        )
        self.kernel.update_memory(
            "autonomy_upgrade_report",
            json.dumps(report, ensure_ascii=False),
        )
        if report.get("summary"):
            self.kernel.update_memory("autonomy_upgrade_summary", report["summary"])

    def _record_autonomy_charter(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection: ReflectionEntry,
        feedback_metrics: Dict[str, Any],
    ) -> None:
        static_report = self._load_json_memory("static_analysis_report")
        charter_config = self._load_json_memory("autonomy_charter_config")
        charter = build_autonomy_charter(
            observations=observations,
            evaluation=evaluation,
            reflection=reflection,
            feedback_metrics=feedback_metrics,
            static_report=static_report,
            config=charter_config,
        )
        self.kernel.update_memory(
            "autonomy_charter",
            json.dumps(charter, ensure_ascii=False),
        )
        self.kernel.update_memory(
            "autonomy_charter_status",
            charter.get("status", "unset"),
        )

    def _load_json_memory(self, key: str) -> Dict[str, Any]:
        raw = self.kernel.state.memory.get(key)
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _safe_mode_active(self) -> bool:
        return self.kernel.state.memory.get("safe_mode") == "true"

    def _load_memory_int(self, key: str, default: int) -> int:
        raw_value = self.kernel.state.memory.get(key)
        if raw_value is None:
            return default
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _last_error_cycle(feedback_log: List[Dict[str, Any]]) -> int | None:
        for entry in reversed(feedback_log):
            if entry.get("errors"):
                return entry.get("cycle")
        return None

    @staticmethod
    def _correction_latency(feedback_log: List[Dict[str, Any]]) -> int | None:
        last_error_cycle = None
        for entry in reversed(feedback_log):
            if entry.get("errors"):
                last_error_cycle = entry.get("cycle")
                break
        if last_error_cycle is None:
            return None
        for entry in feedback_log:
            if entry.get("cycle") > last_error_cycle and not entry.get("errors"):
                return entry["cycle"] - last_error_cycle
        return None

    @staticmethod
    def _hypotheses_review(errors: List[str], correction_latency: int | None) -> str:
        if errors and correction_latency is None:
            return "Hypothesis: current updates are insufficient; revise strategies."
        if errors and correction_latency is not None:
            return "Hypothesis: feedback loop is correcting, continue monitoring."
        return "Hypothesis: no errors detected; validate stability across cycles."
