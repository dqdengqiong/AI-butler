from __future__ import annotations

from datetime import date, timedelta

import pytest

from ai_butler.adapters.llm import FakeLLM
from ai_butler.agent.availability import AvailabilityInterpreter
from ai_butler.tools import (
    DEFAULT_TOOL_REGISTRY,
    PlanningScenarioSpec,
    PlanRequirementCollector,
    schedule_plan_window,
)


@pytest.mark.asyncio
async def test_requirement_collector_is_reused_by_a_second_scenario() -> None:
    collector = PlanRequirementCollector(AvailabilityInterpreter(FakeLLM()))
    scenario = PlanningScenarioSpec(
        code="IELTS",
        label="雅思",
        required_fields=("target_band",),
        field_prompts={"target_band": "目标分数"},
        field_patterns={"target_band": r"(?:目标|考到)\s*(\d(?:\.\d)?)\s*分?"},
        retrieval_query_prefix="雅思备考",
    )
    result = await collector.collect(
        current_input="雅思目标7.5分，准备3个月，每天学习2小时",
        recent_messages=(),
        start_date=date(2026, 8, 19),
        scenario=scenario,
    )
    assert result.status == "COMPLETED"
    assert result.data is not None
    assert result.data.scenario_fields == {"target_band": "7.5"}  # type: ignore[union-attr]


def test_schedule_window_is_deterministic_and_respects_eighty_five_percent() -> None:
    start = date(2026, 8, 17)
    stages = (
        {"stage_key": "stage_1", "start_date": start, "end_date": start + timedelta(days=20)},
    )
    templates = tuple(
        {
            "stage_key": "stage_1",
            "template_key": f"template_{index}",
            "title": f"任务 {index}",
            "expected_minutes": 50,
            "priority": index,
            "frequency": {"days_per_week": 3},
        }
        for index in range(1, 4)
    )
    availability = {
        "weekly_minutes": 700,
        "windows": [{"day_of_week": day, "available_minutes": 100} for day in range(1, 8)],
        "excluded_days": [],
    }
    first = schedule_plan_window(
        revision_ref="revision",
        templates=templates,
        stages=stages,
        availability=availability,
        window_start=start,
        window_end=start + timedelta(days=6),
    )
    second = schedule_plan_window(
        revision_ref="revision",
        templates=templates,
        stages=stages,
        availability=availability,
        window_start=start,
        window_end=start + timedelta(days=6),
    )
    assert first == second
    tasks, unscheduled = first
    assert sum(item.expected_minutes for item in tasks) <= 595
    assert len({item.task_key for item in tasks}) == len(tasks)
    assert unscheduled


def test_schedule_window_respects_explicit_excluded_dates() -> None:
    start = date(2026, 8, 17)
    tasks, _ = schedule_plan_window(
        revision_ref="revision",
        templates=(
            {
                "stage_key": "stage_1",
                "template_key": "template_1",
                "title": "任务",
                "expected_minutes": 30,
                "priority": 1,
                "frequency": {"days_per_week": 3},
            },
        ),
        stages=(
            {"stage_key": "stage_1", "start_date": start, "end_date": start + timedelta(days=6)},
        ),
        availability={
            "weekly_minutes": 420,
            "windows": [{"day_of_week": day, "available_minutes": 60} for day in range(1, 8)],
            "excluded_days": [],
            "excluded_dates": ["2026-08-19"],
        },
        window_start=start,
        window_end=start + timedelta(days=6),
    )
    assert date(2026, 8, 19) not in {task.scheduled_date for task in tasks}


def test_registry_maps_intent_and_context_to_a_fixed_call_plan() -> None:
    assert DEFAULT_TOOL_REGISTRY.resolve("DAILY_PLANNING", ("PLAN_CONTEXT", "TASK_CONTEXT")) == (
        "read_plan_context",
        "read_task_context",
    )
    assert DEFAULT_TOOL_REGISTRY.resolve("GENERAL_CHAT", ()) == ()
