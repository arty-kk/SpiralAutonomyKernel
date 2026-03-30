# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from importlib import import_module
from typing import Any, Dict


STRATEGY_MODULES: Dict[str, Dict[str, str]] = {
    "plan": {
        "default_plan": "evolvable.strategies.plan_strategy:DefaultPlanStrategy",
        "experimental_plan": "evolvable.strategies.plan_strategy:ExperimentalPlanStrategy",
    },
    "evaluation": {
        "default_evaluation": "evolvable.strategies.evaluation_strategy:DefaultEvaluationStrategy",
        "experimental_evaluation": "evolvable.strategies.evaluation_strategy:ExperimentalEvaluationStrategy",
    },
    "reflection": {
        "default_reflection": "evolvable.strategies.reflection_strategy:DefaultReflectionStrategy",
        "experimental_reflection": (
            "evolvable.strategies.reflection_strategy:ExperimentalReflectionStrategy"
        ),
    },
}


def load_strategy(strategy_type: str, name: str | None) -> Any:
    catalog = STRATEGY_MODULES.get(strategy_type, {})
    strategy_name = name if name in catalog else f"default_{strategy_type}"
    target = catalog.get(strategy_name)
    if not target:
        target = next(iter(catalog.values()), "")
    if not target:
        raise ValueError(f"No strategy registered for type '{strategy_type}'.")
    module_path, class_name = target.split(":")
    try:
        module = import_module(module_path)
        strategy_cls = getattr(module, class_name)
        return strategy_cls()
    except Exception as exc:
        if name not in catalog:
            fallback_target = catalog.get(f"default_{strategy_type}")
            if fallback_target and fallback_target != target:
                fallback_module, fallback_class = fallback_target.split(":")
                module = import_module(fallback_module)
                strategy_cls = getattr(module, fallback_class)
                return strategy_cls()
        raise RuntimeError(
            f"Failed to load strategy '{strategy_name}' for type '{strategy_type}': {exc}"
        ) from exc
