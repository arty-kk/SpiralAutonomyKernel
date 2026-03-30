from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import List

from sif.core.evolution import CodeChange


def _hash_code_changes(code_changes: List[CodeChange]) -> str:
    payload = [
        {"path": change.path, "content": change.content, "notes": change.notes}
        for change in sorted(code_changes, key=lambda item: item.path)
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class Candidate:
    code_changes: List[CodeChange]
    source: str
    notes: str = ""
    id: str = field(default_factory=str)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = _hash_code_changes(self.code_changes)
