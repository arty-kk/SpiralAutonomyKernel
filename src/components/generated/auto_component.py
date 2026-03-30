# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from typing import Any, Dict, List

from sif.components.base import Component, ComponentSignal
from sif.core.evolution import CodeChange, KernelUpdate


@dataclass
class AutoComponent(Component):
    async def apply(self, plan: List[str]) -> ComponentSignal:
        _ = plan
        return self.empty_signal()

    async def propose_updates(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection_summary: str,
    ) -> List[KernelUpdate]:
        _ = observations, evaluation, reflection_summary
        return []

    async def propose_code_changes(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection_summary: str,
    ) -> List[CodeChange]:
        _ = observations, evaluation, reflection_summary
        return []
