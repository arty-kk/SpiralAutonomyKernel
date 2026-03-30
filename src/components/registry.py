import logging
from dataclasses import dataclass, field
from importlib import import_module
from inspect import isclass
from pkgutil import iter_modules
from typing import List

from sif.components import generated
from sif.components.adaptation import AdaptationComponent
from sif.components.autonomy_scope import AutonomyScopeComponent
from sif.components.base import Component
from sif.components.code_mutation import CodeMutationComponent
from sif.components.constraint_explorer import ConstraintExplorerComponent
from sif.components.governance import GovernanceComponent
from sif.components.improvement_protocol import ImprovementProtocolComponent
from sif.components.mission_alignment import MissionAlignmentComponent

logger = logging.getLogger(__name__)


@dataclass
class ComponentRegistry:
    components: List[Component] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.components:
            self.components.append(GovernanceComponent(name='governance'))
            self.components.append(ImprovementProtocolComponent(name='improvement_protocol'))
            self.components.append(AdaptationComponent(name='adaptation'))
            self.components.append(ConstraintExplorerComponent(name='constraint_explorer'))
            self.components.append(MissionAlignmentComponent(name='mission_alignment'))
            self.components.append(AutonomyScopeComponent(name='autonomy_scope'))
            self.components.append(CodeMutationComponent(name='code_mutation'))
            self.components.extend(self._load_generated_components())

    def get_component(self, name: str) -> Component | None:
        for component in self.components:
            if component.name == name:
                return component
        return None

    @staticmethod
    def _load_generated_components() -> List[Component]:
        components: List[Component] = []
        for module_info in iter_modules(generated.__path__, f'{generated.__name__}.'):
            try:
                module = import_module(module_info.name)
            except Exception:
                logger.warning('Failed to import generated component module %s', module_info.name, exc_info=True)
                continue
            for attribute in module.__dict__.values():
                if isclass(attribute) and issubclass(attribute, Component) and attribute is not Component:
                    try:
                        instance = attribute(name=attribute.__name__.lower())
                    except Exception:
                        logger.warning('Failed to instantiate component %s from module %s', attribute.__name__, module.__name__, exc_info=True)
                        continue
                    components.append(instance)
        return components
