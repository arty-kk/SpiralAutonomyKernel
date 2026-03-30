# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
from sif.core.time_utils import utc_now_iso

from dataclasses import dataclass, field
import json
import math
import random
from typing import Any, Dict, Iterable, Tuple

SCHEMA_VERSION = "v2"
MIN_EPSILON = 0.1
MAX_EPSILON = 0.5


@dataclass
class BanditState:
    counts: Dict[str, int] = field(default_factory=dict)
    values: Dict[str, float] = field(default_factory=dict)
    last_update: str | None = None
    epsilon: float = MIN_EPSILON
    schema_version: str = SCHEMA_VERSION


def _coerce_int_map(raw: Any) -> Dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    converted: Dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            return None
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return None
        if numeric < 0:
            return None
        converted[key] = numeric
    return converted


def _coerce_float_map(raw: Any) -> Dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    converted: Dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        converted[key] = numeric
    return converted


def load_bandit_state(raw_state: str | None) -> Tuple[BanditState, bool]:
    if not raw_state:
        return BanditState(), False
    try:
        payload = json.loads(raw_state)
    except json.JSONDecodeError:
        return BanditState(), False
    if not isinstance(payload, dict):
        return BanditState(), False
    counts = _coerce_int_map(payload.get("counts", {}))
    values = _coerce_float_map(payload.get("values", {}))
    if counts is None or values is None:
        return BanditState(), False
    last_update = payload.get("last_update")
    if last_update is not None and not isinstance(last_update, str):
        return BanditState(), False
    schema_version = payload.get("schema_version") or SCHEMA_VERSION
    if not isinstance(schema_version, str):
        return BanditState(), False
    epsilon = payload.get("epsilon", MIN_EPSILON)
    try:
        epsilon = float(epsilon)
    except (TypeError, ValueError):
        return BanditState(), False
    epsilon = min(max(epsilon, MIN_EPSILON), MAX_EPSILON)
    return BanditState(
        counts=counts,
        values=values,
        last_update=last_update,
        epsilon=epsilon,
        schema_version=schema_version,
    ), True


def serialize_bandit_state(state: BanditState) -> str:
    payload = {
        "counts": state.counts,
        "values": state.values,
        "last_update": state.last_update,
        "epsilon": state.epsilon,
        "schema_version": state.schema_version,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _has_data(state: BanditState) -> bool:
    return any(count > 0 for count in state.counts.values()) and bool(state.values)


def select_action(
    arms: Iterable[str],
    state: BanditState,
    epsilon: float | None = None,
) -> Tuple[str | None, str]:
    arm_list = [arm for arm in arms if isinstance(arm, str)]
    if not arm_list:
        return None, "no_arms"
    if not _has_data(state):
        return None, "no_data"
    epsilon_value = state.epsilon if epsilon is None else epsilon
    epsilon_value = min(max(epsilon_value, MIN_EPSILON), MAX_EPSILON)
    if random.random() < epsilon_value:
        return random.choice(arm_list), "epsilon_exploration"
    untried = [arm for arm in arm_list if state.counts.get(arm, 0) == 0]
    if untried:
        return random.choice(untried), "ucb_untried"
    total = sum(state.counts.get(arm, 0) for arm in arm_list)
    if total <= 0:
        return None, "no_counts"
    best_arm = None
    best_score = -float("inf")
    for arm in arm_list:
        count = state.counts.get(arm, 0)
        value = state.values.get(arm, 0.0)
        if count <= 0:
            score = float("inf")
        else:
            score = value + math.sqrt(2 * math.log(total) / count)
        if score > best_score:
            best_score = score
            best_arm = arm
    return best_arm, "ucb1"


def update_state(state: BanditState, arm: str, reward: float) -> BanditState:
    if not isinstance(arm, str) or not arm:
        return state
    reward = min(max(reward, 0.0), 1.0)
    count = state.counts.get(arm, 0)
    value = state.values.get(arm, 0.0)
    new_count = count + 1
    new_value = (value * count + reward) / new_count
    state.counts[arm] = new_count
    state.values[arm] = new_value
    state.last_update = utc_now_iso(timespec="seconds")
    return state


def evolve_state(state: BanditState, performance_signal: float) -> BanditState:
    performance_signal = min(max(performance_signal, 0.0), 1.0)
    if performance_signal >= 0.8:
        state.epsilon = max(MIN_EPSILON, state.epsilon - 0.02)
    elif performance_signal <= 0.4:
        state.epsilon = min(MAX_EPSILON, state.epsilon + 0.05)
    else:
        state.epsilon = min(MAX_EPSILON, max(MIN_EPSILON, state.epsilon))
    return state


def suggest_focus(
    arms: Iterable[str],
    state: BanditState,
    min_count: int = 1,
) -> str | None:
    if not _has_data(state):
        return None
    candidates = [
        arm for arm in arms if state.counts.get(arm, 0) >= min_count and isinstance(arm, str)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda arm: state.values.get(arm, 0.0))
