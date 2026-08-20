from __future__ import annotations

import pytest

from ai_butler.adapters.embedding import FakeEmbeddingProvider
from ai_butler.application.butler.memory import (
    BUSINESS_ENTITY_PATTERN,
    SENSITIVE_PATTERN,
    TEMPORARY_PATTERN,
    LongTermMemoryService,
)
from ai_butler.application.butler.memory.store import store_index_config
from ai_butler.config import Settings


def test_context_thresholds_are_ordered_and_capped() -> None:
    settings = Settings(context_soft_limit_ratio=0.7, context_hard_limit_ratio=0.85)
    assert settings.router_context_hard_tokens == 4_000
    assert settings.general_context_hard_tokens == 8_000
    assert settings.planning_context_hard_tokens == 10_000
    assert settings.research_context_hard_tokens == 12_000
    assert settings.message_input_max_tokens == 1_800
    with pytest.raises(ValueError, match=r"0 < soft < hard <= 0\.95"):
        Settings(context_soft_limit_ratio=0.9, context_hard_limit_ratio=0.8)
    with pytest.raises(ValueError, match=r"0 < soft < hard <= 0\.95"):
        Settings(context_soft_limit_ratio=0.7, context_hard_limit_ratio=0.96)


def test_memory_policy_inputs_are_deterministic() -> None:
    assert LongTermMemoryService._category("我通常早上学习") == "HABIT"
    assert LongTermMemoryService._category("我的学历背景是本科") == "BACKGROUND"
    assert LongTermMemoryService._category("周末不能安排任务") == "CONSTRAINT"
    assert SENSITIVE_PATTERN.search("记住我的银行卡")
    assert TEMPORARY_PATTERN.search("我今天想学习")
    assert BUSINESS_ENTITY_PATTERN.search("plan_id=abc")
    assert LongTermMemoryService._slot_key("我通常早上学习", "HABIT") == "habit:早上学习"
    index = store_index_config(FakeEmbeddingProvider())
    assert index["dims"] == 1024
    assert index["fields"] == ["statement"]
    assert index["distance_type"] == "cosine"
