"""长期记忆候选结构与确定性准入策略。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    normalized_key: str
    value: str
    category: Literal["PREFERENCE", "HABIT", "CONSTRAINT", "BACKGROUND"]
    explicit: float
    stable: float
    useful: float
    specific: float
    repeated: float
    user_requested: bool = False
    sensitive: bool = False


class MemoryPolicy:
    """模型只能提出候选；确定性 Policy 决定准入与 TTL。"""

    def __init__(self, preference_ttl_days: int = 180, constraint_ttl_days: int = 365) -> None:
        self.preference_ttl_days = preference_ttl_days
        self.constraint_ttl_days = constraint_ttl_days

    @staticmethod
    def score(candidate: MemoryCandidate) -> float:
        return (
            0.35 * candidate.explicit
            + 0.25 * candidate.stable
            + 0.20 * candidate.useful
            + 0.10 * candidate.specific
            + 0.10 * candidate.repeated
        )

    def admit(self, candidate: MemoryCandidate) -> tuple[bool, int | None]:
        if candidate.sensitive or not candidate.normalized_key or not candidate.value:
            return False, None
        threshold = 0.60 if candidate.user_requested else 0.75
        if self.score(candidate) < threshold:
            return False, None
        ttl = (
            self.preference_ttl_days
            if candidate.category in {"PREFERENCE", "HABIT"}
            else self.constraint_ttl_days
        )
        return True, ttl
