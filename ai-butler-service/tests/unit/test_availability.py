from __future__ import annotations

import json
from datetime import date

import pytest

from ai_butler.adapters.llm import (
    FakeLLM,
    ModelRequest,
    ModelResponse,
    ModelServerError,
)
from ai_butler.agent.availability import (
    AvailabilityInterpretationV1,
    AvailabilityInterpreter,
    AvailabilityWindowV1,
    quick_availability_options,
)
from ai_butler.application.butler import ButlerService


class InvalidLLM:
    async def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            provider="test",
            model="invalid",
            model_profile="invalid",
            content="not-json",
            prompt_version=request.prompt_version,
            attempt=request.attempt_offset + 1,
        )


class FailingLLM:
    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise ModelServerError("unavailable")


@pytest.mark.asyncio
async def test_natural_language_exclusion_overrides_every_day() -> None:
    result = await AvailabilityInterpreter(FakeLLM()).interpret("每天 1 个小时，周末不学习")

    assert result.status == "COMPLETE"
    assert result.weekly_minutes == 300
    assert [window.day_of_week for window in result.windows] == [1, 2, 3, 4, 5]
    assert result.excluded_days == (6, 7)
    assert result.summary == "周一至周五每天 1 小时，周六、周日休息，每周共 5 小时"


@pytest.mark.asyncio
async def test_weekly_total_does_not_invent_days() -> None:
    result = await AvailabilityInterpreter(FakeLLM()).interpret("每周 6 小时")

    assert result.status == "COMPLETE"
    assert result.weekly_minutes == 360
    assert result.windows == ()
    assert result.summary == "每周最多 6 小时，具体学习日灵活安排"


@pytest.mark.asyncio
async def test_invalid_model_output_is_repaired_once_then_clarified() -> None:
    result = await AvailabilityInterpreter(InvalidLLM()).interpret("模糊表达")

    assert result.status == "NEEDS_CLARIFICATION"
    assert result.question == "我没能准确理解这段时间安排，请换一种具体说法。"


def test_quick_options_have_stable_normalized_values() -> None:
    options = quick_availability_options()

    assert [item["id"] for item in options] == [
        "weekday-daily-30",
        "weekday-daily-60",
        "daily-60",
        "weekend-daily-120",
    ]
    assert [item["availability"]["weekly_minutes"] for item in options] == [150, 300, 420, 240]  # type: ignore[index]


def test_overlapping_windows_require_clarification() -> None:
    result = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            windows=(
                AvailabilityWindowV1(
                    day_of_week=1,
                    available_minutes=60,
                    start_time="20:00:00",
                    end_time="21:00:00",
                ),
                AvailabilityWindowV1(
                    day_of_week=1,
                    available_minutes=60,
                    start_time="20:30:00",
                    end_time="21:30:00",
                ),
            ),
        )
    )

    assert result.status == "NEEDS_CLARIFICATION"
    assert "重叠" in str(result.question)


def test_model_schema_is_json_serializable() -> None:
    option = quick_availability_options()[0]
    assert json.loads(json.dumps(option, ensure_ascii=False))["id"] == "weekday-daily-30"


def test_explicit_time_window_recomputes_duration_and_summarizes_single_day() -> None:
    result = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            windows=(
                AvailabilityWindowV1(
                    day_of_week=3,
                    available_minutes=10,
                    start_time="20:00:00",
                    end_time="21:30:00",
                ),
            ),
        )
    )

    assert result.weekly_minutes == 90
    assert result.windows[0].available_minutes == 90
    assert result.summary == "周三 1 小时 30 分钟，每周共 1 小时 30 分钟"


def test_weekend_quick_option_has_weekend_summary() -> None:
    option = quick_availability_options()[3]["availability"]
    result = AvailabilityInterpretationV1.model_validate(option)

    assert result.summary == "周末每天 2 小时，周一至周五休息，每周共 4 小时"


def test_task_draft_uses_only_available_days_and_caps_duration() -> None:
    availability = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            windows=tuple(
                AvailabilityWindowV1(day_of_week=day, available_minutes=20) for day in range(1, 6)
            ),
            excluded_days=(6, 7),
        )
    )
    tasks = ButlerService._draft_tasks_for_availability(date(2026, 8, 15), availability)

    assert [task["day_offset"] for task in tasks] == [2, 3, 4]
    assert all(task["minutes"] == 20 for task in tasks)


@pytest.mark.asyncio
async def test_fake_parser_supports_workdays_weekends_and_minutes() -> None:
    interpreter = AvailabilityInterpreter(FakeLLM())

    workdays = await interpreter.interpret("工作日每天 30 分钟")
    weekend = await interpreter.interpret("周末每天 2 小时")

    assert workdays.weekly_minutes == 150
    assert workdays.excluded_days == (6, 7)
    assert weekend.weekly_minutes == 240
    assert weekend.excluded_days == (1, 2, 3, 4, 5)


@pytest.mark.parametrize(
    "window",
    [
        {"day_of_week": 1, "available_minutes": 60, "start_time": "20:00:00"},
        {
            "day_of_week": 1,
            "available_minutes": 60,
            "start_time": "21:00:00",
            "end_time": "20:00:00",
        },
    ],
)
def test_invalid_time_window_is_rejected(window: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AvailabilityWindowV1.model_validate(window)


@pytest.mark.asyncio
async def test_model_infrastructure_failure_propagates_for_worker_retry() -> None:
    with pytest.raises(ModelServerError):
        await AvailabilityInterpreter(FailingLLM()).interpret("每天一小时")


def test_normalizer_rejects_invalid_days_duplicate_defaults_and_empty_input() -> None:
    invalid_day = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(status="COMPLETE", excluded_days=(8,), weekly_minutes=60)
    )
    duplicate = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            windows=(
                AvailabilityWindowV1(day_of_week=1, available_minutes=30),
                AvailabilityWindowV1(day_of_week=1, available_minutes=30),
            ),
        )
    )
    empty = AvailabilityInterpreter.normalize(AvailabilityInterpretationV1(status="COMPLETE"))
    model_question = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(status="NEEDS_CLARIFICATION")
    )

    assert invalid_day.status == duplicate.status == empty.status == "NEEDS_CLARIFICATION"
    assert model_question.question == "请说明每周总时长，或具体哪些天可以学习。"


def test_summary_lists_custom_excluded_days() -> None:
    result = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            windows=(AvailabilityWindowV1(day_of_week=2, available_minutes=30),),
            excluded_days=(1,),
        )
    )
    assert result.summary == "周二 30 分钟，周一休息，每周共 30 分钟"
