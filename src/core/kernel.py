# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass, field
from typing import Any, Dict, List

from sif.core.reflection import ConstraintAssessment, ReflectionLog

MEMORY_SCHEMA_VERSION = "v1"
METRICS_V1_NAMESPACE = "sif.metrics.v1"
REPORTS_V1_NAMESPACE = "sif.reports.v1"


@dataclass
class KernelState:
    """State container for the autonomy kernel."""

    goals: List[str]
    constraints: List[str]
    memory: Dict[str, str] = field(default_factory=dict)


@dataclass
class Kernel:
    """Holds core state and reflection history."""

    state: KernelState
    reflections: ReflectionLog = field(default_factory=ReflectionLog)

    def update_memory(self, key: str, value: str) -> None:
        self.state.memory[key] = value

    def update_memory_many(self, updates: Dict[str, str]) -> None:
        if not updates:
            return
        next_memory = dict(self.state.memory)
        next_memory.update(updates)
        self.state.memory = next_memory

    def record_reflection(
        self,
        summary: str,
        constraints: List[ConstraintAssessment] | None = None,
        opportunities: List[str] | None = None,
        assumptions: List[str] | None = None,
        ignored_directives: List[str] | None = None,
        dod: Dict[str, Any] | None = None,
        dod_check: Dict[str, Any] | None = None,
    ) -> None:
        self.reflections.add_entry(
            summary,
            constraints=constraints,
            opportunities=opportunities,
            assumptions=assumptions,
            ignored_directives=ignored_directives,
            dod=dod,
            dod_check=dod_check,
        )
