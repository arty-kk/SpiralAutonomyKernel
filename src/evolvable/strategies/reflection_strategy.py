# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from sif.core.spiral_engine import SpiralEngine
    from sif.core.reflection import ReflectionEntry


@dataclass
class ReflectionStrategy:
    name: str = "base_reflection"

    async def reflect(
        self,
        engine: "SpiralEngine",
        evaluation: Dict[str, Any],
    ) -> "ReflectionEntry":
        raise NotImplementedError


@dataclass
class DefaultReflectionStrategy(ReflectionStrategy):
    name: str = "default_reflection"

    async def reflect(
        self,
        engine: "SpiralEngine",
        evaluation: Dict[str, Any],
    ) -> "ReflectionEntry":
        return await engine._reflect_impl(evaluation=evaluation)


@dataclass
class ExperimentalReflectionStrategy(ReflectionStrategy):
    name: str = "experimental_reflection"

    async def reflect(
        self,
        engine: "SpiralEngine",
        evaluation: Dict[str, Any],
    ) -> "ReflectionEntry":
        _ = evaluation
        engine.kernel.record_reflection("Experimental reflection placeholder.")
        return engine.kernel.reflections.latest()
