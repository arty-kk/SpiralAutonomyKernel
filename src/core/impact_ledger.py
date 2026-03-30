# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from sif.core.evolution import CodeChange, KernelUpdate
from sif.core.time_utils import utc_now_iso


@dataclass
class ImpactEntry:
    timestamp: str
    cycle_index: int
    updates: List[str]
    code_changes: List[str]
    evaluation_snapshot: Dict[str, Any]
    feedback_snapshot: Dict[str, Any]
    stability_signal: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cycle_index": self.cycle_index,
            "updates": self.updates,
            "code_changes": self.code_changes,
            "evaluation": self.evaluation_snapshot,
            "feedback": self.feedback_snapshot,
            "stability_signal": self.stability_signal,
        }


def build_impact_entry(
    cycle_index: int,
    updates_applied: List[KernelUpdate],
    code_changes_applied: List[CodeChange],
    evaluation: Dict[str, Any],
    feedback_metrics: Dict[str, Any],
) -> ImpactEntry:
    stability_signal = "stable"
    if evaluation.get("errors"):
        stability_signal = "degraded"
    elif evaluation.get("alignment") == "partial":
        stability_signal = "partial"
    elif evaluation.get("alignment") == "at_risk":
        stability_signal = "at_risk"
    return ImpactEntry(
        timestamp=utc_now_iso(timespec="seconds"),
        cycle_index=cycle_index,
        updates=[update.action for update in updates_applied],
        code_changes=[change.path for change in code_changes_applied],
        evaluation_snapshot={
            "alignment": evaluation.get("alignment"),
            "coverage": evaluation.get("coverage"),
            "notes": evaluation.get("notes"),
        },
        feedback_snapshot={
            "accuracy_signal": feedback_metrics.get("accuracy_signal"),
            "adaptation_signal": feedback_metrics.get("adaptation_signal"),
            "goal_alignment_score": feedback_metrics.get("goal_alignment_score"),
            "goal_coverage_ratio": feedback_metrics.get("goal_coverage_ratio"),
        },
        stability_signal=stability_signal,
    )


def append_impact_log(
    existing_log: List[Dict[str, Any]],
    entry: ImpactEntry,
) -> List[Dict[str, Any]]:
    existing_log.append(entry.to_dict())
    return existing_log
