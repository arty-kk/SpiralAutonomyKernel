# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from typing import List


@dataclass
class ImprovementStage:
    name: str
    intent: str
    outputs: List[str]


@dataclass
class ImprovementProtocol:
    name: str
    stages: List[ImprovementStage]
    self_model: List[str]

    def describe(self) -> str:
        stage_names = ', '.join(stage.name for stage in self.stages)
        self_model = '; '.join(self.self_model)
        return f"{self.name}: stages=[{stage_names}]; self_model=[{self_model}]"
