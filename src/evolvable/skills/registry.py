# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Iterable, List

from sif.components.registry import ComponentRegistry
from sif.evolvable.skills.component_adapter import build_component_skills
from sif.evolvable.skills.skill_base import Skill


@dataclass
class SkillRegistry:
    component_registry: ComponentRegistry = field(default_factory=ComponentRegistry)
    allowlist_modules: Iterable[str] = field(
        default_factory=lambda: ["evolvable.skills.component_adapter"]
    )

    def load_skills(self) -> List[Skill]:
        skills: List[Skill] = []
        skills.extend(build_component_skills(self.component_registry))
        for module_path in self.allowlist_modules:
            if module_path == "evolvable.skills.component_adapter":
                continue
            try:
                module = import_module(module_path)
            except Exception:
                continue
            register = getattr(module, "register_skills", None)
            if callable(register):
                module_skills = register()
                if isinstance(module_skills, list):
                    skills.extend([skill for skill in module_skills if isinstance(skill, Skill)])
        return skills
