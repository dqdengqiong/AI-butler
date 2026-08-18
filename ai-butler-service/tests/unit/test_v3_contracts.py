from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_butler.api.schemas import PlanCardPayloadV11
from ai_butler.application.butler.memory import (
    BUSINESS_ENTITY_PATTERN,
    SENSITIVE_PATTERN,
    TEMPORARY_PATTERN,
    LongTermMemoryService,
)
from ai_butler.application.butler.plan_execution import PlanExecutionService
from ai_butler.config import Settings


def test_context_thresholds_are_ordered_and_capped() -> None:
    assert Settings(context_soft_limit_ratio=0.7, context_hard_limit_ratio=0.85)
    with pytest.raises(ValueError, match=r"0 < soft < hard <= 0\.95"):
        Settings(context_soft_limit_ratio=0.9, context_hard_limit_ratio=0.8)
    with pytest.raises(ValueError, match=r"0 < soft < hard <= 0\.95"):
        Settings(context_soft_limit_ratio=0.7, context_hard_limit_ratio=0.96)


def test_plan_card_11_enforces_bundle_and_adjust_cardinality() -> None:
    base_plan = {
        "work_item_id": "work-1",
        "plan_id": "00000000-0000-4000-8000-000000000001",
        "plan_revision_id": "00000000-0000-4000-8000-000000000002",
        "title": "计划",
        "objective_summary": "目标",
        "weekly_minutes": 60,
    }
    with pytest.raises(ValidationError, match="at least two"):
        PlanCardPayloadV11(
            mode="BUNDLE_CREATE",
            title="组合",
            plans=[base_plan],  # type: ignore[list-item]
            total_weekly_minutes=60,
            available_weekly_minutes=100,
        )
    with pytest.raises(ValidationError, match="exactly one"):
        PlanCardPayloadV11(
            mode="SINGLE_PLAN_ADJUST",
            title="调整",
            plans=[],
            total_weekly_minutes=60,
            available_weekly_minutes=100,
        )


def test_bundle_split_and_memory_policy_inputs_are_deterministic() -> None:
    objectives = PlanExecutionService._work_item_objectives("制定省考计划；另外制定申论专项计划")
    assert objectives == ("制定省考计划", "制定申论专项计划")
    assert PlanExecutionService._work_item_objectives("制定省考计划") == ("制定省考计划",)
    assert LongTermMemoryService._category("我通常早上学习") == "HABIT"
    assert LongTermMemoryService._category("我的学历背景是本科") == "BACKGROUND"
    assert LongTermMemoryService._category("周末不能安排任务") == "CONSTRAINT"
    assert SENSITIVE_PATTERN.search("记住我的银行卡")
    assert TEMPORARY_PATTERN.search("我今天想学习")
    assert BUSINESS_ENTITY_PATTERN.search("plan_id=abc")
