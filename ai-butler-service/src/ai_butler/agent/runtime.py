"""上下文预算的确定性安全边界。"""

from __future__ import annotations

from ai_butler.agent.contracts import ContextBundleV1, ContextItemV1
from ai_butler.domain.errors import ButlerError


class ContextBudgetGuard:
    """按固定优先级裁剪外部证据、记忆和旧消息，保留身份与当前输入。"""

    def __init__(self, max_tokens: int, hard_tokens: int | None = None) -> None:
        if max_tokens < 256:
            raise ValueError("context budget is too small")
        if hard_tokens is not None and hard_tokens < max_tokens:
            raise ValueError("hard context budget must not be smaller than target")
        self.max_tokens = max_tokens
        self.hard_tokens = hard_tokens or max_tokens

    def compact(self, bundle: ContextBundleV1) -> ContextBundleV1:
        required = bundle.current_input.estimated_tokens + sum(
            item.estimated_tokens for item in bundle.business_facts
        )
        if required > self.hard_tokens:
            raise ButlerError("CONTEXT_REQUIRED_OVER_BUDGET", "必要上下文超过模型预算", 422)
        # 必要事实可从正常目标扩展至硬上限；可选上下文只使用正常目标的余量。
        remaining = max(0, self.max_tokens - required)
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
