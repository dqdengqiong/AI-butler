"""上下文预算与记忆确定性策略。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ai_butler.agent.contracts import ContextBundleV1, ContextItemV1
from ai_butler.domain.errors import ButlerError


class ContextBudgetGuard:
    """按固定优先级裁剪外部证据、记忆和旧消息，保留身份与当前输入。"""

    def __init__(self, max_tokens: int) -> None:
        if max_tokens < 256:
            raise ValueError("context budget is too small")
        self.max_tokens = max_tokens

    def compact(self, bundle: ContextBundleV1) -> ContextBundleV1:
        required = bundle.current_input.estimated_tokens + sum(
            item.estimated_tokens for item in bundle.business_facts
        )
        if required > self.max_tokens:
            raise ButlerError("CONTEXT_REQUIRED_OVER_BUDGET", "必要上下文超过模型预算", 422)
        remaining = self.max_tokens - required
        selected: dict[str, tuple[ContextItemV1, ...]] = {}
        for field, items in (
            ("summaries", bundle.summaries),
            ("messages", tuple(reversed(bundle.messages))),
            ("memories", bundle.memories),
            ("evidence", bundle.evidence),
        ):
            kept: list[ContextItemV1] = []
            for item in items:
                if item.estimated_tokens <= remaining:
                    kept.append(item)
                    remaining -= item.estimated_tokens
            selected[field] = tuple(reversed(kept)) if field == "messages" else tuple(kept)
        return bundle.model_copy(update=selected)


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
        ttl = 180 if candidate.category in {"PREFERENCE", "HABIT"} else 365
        return True, ttl
