# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


def coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class StateNode:
    name: str
    description: str
    allowed_actions: List[str]


@dataclass
class StateTransition:
    from_state: str
    to_state: str
    trigger: str


@dataclass
class StateOntology:
    states: Dict[str, StateNode]
    transitions: List[StateTransition]
    current_state: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_state": self.current_state,
            "states": {
                name: {
                    "description": node.description,
                    "allowed_actions": node.allowed_actions,
                }
                for name, node in self.states.items()
            },
            "transitions": [
                {
                    "from": transition.from_state,
                    "to": transition.to_state,
                    "trigger": transition.trigger,
                }
                for transition in self.transitions
            ],
        }


def build_state_ontology(
    evaluation: Dict[str, Any],
    feedback_metrics: Dict[str, Any] | None = None,
    constraints: List[str] | None = None,
) -> StateOntology:
    feedback_metrics = feedback_metrics or {}
    constraints = constraints or []
    errors = evaluation.get("errors") or []
    alignment = evaluation.get("alignment")
    accuracy_signal = coerce_float(feedback_metrics.get("accuracy_signal", 1.0), 1.0)
    adaptation_signal = coerce_float(feedback_metrics.get("adaptation_signal", 1.0), 1.0)
    accuracy_signal = min(max(accuracy_signal, 0.0), 1.0)
    adaptation_signal = min(max(adaptation_signal, 0.0), 1.0)
    constraint_pressure = len(constraints)

    if errors or alignment == "at_risk":
        current_state = "recovering"
    elif accuracy_signal < 1.0 or alignment == "partial":
        current_state = "stabilizing"
    elif adaptation_signal == 0.0:
        current_state = "exploring"
    else:
        current_state = "steady"

    states = {
        "steady": StateNode(
            name="steady",
            description="Stable performance with balanced adaptation.",
            allowed_actions=["optimize", "observe", "experiment"],
        ),
        "stabilizing": StateNode(
            name="stabilizing",
            description="Metrics show drift; tighten execution.",
            allowed_actions=["stabilize", "audit", "reduce_risk"],
        ),
        "exploring": StateNode(
            name="exploring",
            description="Low adaptation signal; encourage exploration.",
            allowed_actions=["explore", "prototype", "broaden_scope"],
        ),
        "recovering": StateNode(
            name="recovering",
            description="Errors or at-risk alignment; prioritize recovery.",
            allowed_actions=["rollback", "safeguard", "restore"],
        ),
    }
    if constraint_pressure:
        states["stabilizing"].allowed_actions.append("reframe_constraints")

    transitions = [
        StateTransition("steady", "stabilizing", "accuracy_signal_drop"),
        StateTransition("steady", "exploring", "adaptation_signal_zero"),
        StateTransition("steady", "recovering", "errors_detected"),
        StateTransition("stabilizing", "steady", "accuracy_signal_restored"),
        StateTransition("exploring", "steady", "adaptation_signal_positive"),
        StateTransition("recovering", "stabilizing", "errors_cleared"),
    ]
    return StateOntology(states=states, transitions=transitions, current_state=current_state)


def is_action_allowed(state: StateOntology, action: str) -> bool:
    node = state.states.get(state.current_state)
    if not node:
        return False
    return action in node.allowed_actions
