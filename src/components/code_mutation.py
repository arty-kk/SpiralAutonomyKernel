# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from sif.components.base import Component, ComponentSignal
from sif.core import async_fs
from sif.core.evolution import CodeChange

GENERATED_PATH = Path('src/components/generated/auto_component.py')


@dataclass
class CodeMutationComponent(Component):
    async def apply(self, plan: List[str]) -> ComponentSignal:
        _ = plan
        return self.empty_signal()

    async def propose_code_changes(
        self,
        observations: Dict[str, str],
        evaluation: Dict[str, Any],
        reflection_summary: str,
    ) -> List[CodeChange]:
        _ = observations, evaluation, reflection_summary
        template = '''from dataclasses import dataclass
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
'''
        if await async_fs.exists(GENERATED_PATH):
            existing = await async_fs.read_text(GENERATED_PATH, encoding='utf-8')
            if existing == template:
                return []
        return [
            CodeChange(
                path=str(GENERATED_PATH),
                content=template,
                notes='Seed a generated component surface for future bounded experiments.',
            )
        ]
