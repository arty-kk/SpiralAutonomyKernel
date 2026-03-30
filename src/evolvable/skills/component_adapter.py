from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

from sif.components.base import ComponentSignal
from sif.components.registry import ComponentRegistry
from sif.evolvable.skills.skill_base import Skill


@dataclass
class ComponentSkill(Skill):
    component_name: str
    registry: ComponentRegistry

    def apply(self, plan: List[str], **kwargs: Any) -> ComponentSignal:
        _ = plan, kwargs
        raise RuntimeError("ComponentSkill.apply is async-only; use apply_async instead.")

    async def apply_async(self, plan: List[str], **kwargs: Any) -> ComponentSignal:
        _ = kwargs
        component = self.registry.get_component(self.component_name)
        if component is None:
            raise ValueError(f"Component not found: {self.component_name}")
        return await component.apply(plan)


def build_component_skills(registry: ComponentRegistry) -> List[Skill]:
    skills: List[Skill] = []
    for component in registry.components:
        skills.append(
            ComponentSkill(
                name=f"component:{component.name}",
                description=f"Adapter skill for component {component.name}.",
                component_name=component.name,
                registry=registry,
            )
        )
    return skills
