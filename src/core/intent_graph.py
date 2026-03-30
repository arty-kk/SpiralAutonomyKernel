# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class IntentNode:
    node_id: str
    kind: str
    label: str
    metadata: Dict[str, Any]


@dataclass
class IntentEdge:
    source: str
    target: str
    relation: str


@dataclass
class IntentGraph:
    nodes: Dict[str, IntentNode]
    edges: List[IntentEdge]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": {
                node_id: {
                    "kind": node.kind,
                    "label": node.label,
                    "metadata": node.metadata,
                }
                for node_id, node in self.nodes.items()
            },
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "relation": edge.relation,
                }
                for edge in self.edges
            ],
        }

    def validate(self) -> List[str]:
        errors: List[str] = []
        for edge in self.edges:
            if edge.source not in self.nodes:
                errors.append(f"missing_source:{edge.source}")
            if edge.target not in self.nodes:
                errors.append(f"missing_target:{edge.target}")
        return errors


def build_intent_graph(
    goals: List[str],
    plan: List[str],
    evaluation: Dict[str, Any] | None = None,
) -> IntentGraph:
    evaluation = evaluation or {}
    nodes: Dict[str, IntentNode] = {}
    edges: List[IntentEdge] = []

    for index, goal in enumerate(goals, start=1):
        node_id = f"goal:{index}"
        nodes[node_id] = IntentNode(
            node_id=node_id,
            kind="goal",
            label=goal,
            metadata={},
        )

    for index, action in enumerate(plan, start=1):
        node_id = f"action:{index}"
        nodes[node_id] = IntentNode(
            node_id=node_id,
            kind="action",
            label=action,
            metadata={},
        )

    criteria = {
        "alignment": evaluation.get("alignment"),
        "coverage": evaluation.get("coverage"),
        "notes": evaluation.get("notes"),
    }
    nodes["criteria:alignment"] = IntentNode(
        node_id="criteria:alignment",
        kind="criterion",
        label="alignment",
        metadata=criteria,
    )

    for goal_index in range(1, len(goals) + 1):
        goal_id = f"goal:{goal_index}"
        for action_index in range(1, len(plan) + 1):
            action_id = f"action:{action_index}"
            edges.append(IntentEdge(goal_id, action_id, "drives"))

    for action_index in range(1, len(plan) + 1):
        action_id = f"action:{action_index}"
        edges.append(IntentEdge(action_id, "criteria:alignment", "measured_by"))

    return IntentGraph(nodes=nodes, edges=edges)
