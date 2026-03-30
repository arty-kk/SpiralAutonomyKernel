from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List


@dataclass
class Skill:
    name: str
    description: str

    def can_apply(self, plan: List[str], **kwargs: Any) -> bool:
        _ = plan, kwargs
        return True

    def apply(self, plan: List[str], **kwargs: Any) -> Any:
        raise NotImplementedError

    async def apply_async(self, plan: List[str], **kwargs: Any) -> Any:
        return self.apply(plan, **kwargs)
