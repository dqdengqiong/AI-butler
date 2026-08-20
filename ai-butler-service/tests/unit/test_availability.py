from __future__ import annotations

import json
from datetime import date
from pathlib import Path

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
    expand_availability_calendar,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "days", "daily_minutes"),
    [
        ("每天学习2小时", tuple(range(1, 8)), 120),
        ("每日两小时", tuple(range(1, 8)), 120),
        ("每天半小时", tuple(range(1, 8)), 30),
        ("每天一个半小时", tuple(range(1, 8)), 90),
        ("平日每天1小时", tuple(range(1, 6)), 60),
        ("周一至周五每天1小时", tuple(range(1, 6)), 60),
        ("周末每天2小时", (6, 7), 120),
        ("周一周三周五各45分钟", (1, 3, 5), 45),
    ],
)
async def test_common_chinese_availability_expressions(
    value: str, days: tuple[int, ...], daily_minutes: int
) -> None:
    result = await AvailabilityInterpreter(FakeLLM()).interpret(value)

    assert result.status == "COMPLETE"
    assert tuple(window.day_of_week for window in result.windows) == days
    assert all(window.available_minutes == daily_minutes for window in result.windows)
    assert result.weekly_minutes == len(days) * daily_minutes


@pytest.mark.asyncio
async def test_explicit_time_range_and_exclusion_are_expanded_deterministically() -> None:
    result = await AvailabilityInterpreter(FakeLLM()).interpret("每天晚上7点到9点，周三休息")

    assert result.status == "COMPLETE"
    assert tuple(window.day_of_week for window in result.windows) == (1, 2, 4, 5, 6, 7)
    assert all(str(window.start_time) == "19:00:00" for window in result.windows)
    assert all(str(window.end_time) == "21:00:00" for window in result.windows)
    assert result.weekly_minutes == 720
    assert result.excluded_days == (3,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "question"),
    [
        ("每天", "每天可以学习多少小时或分钟"),
        ("每天1到2小时", "明确要按多少分钟"),
        ("每天有空就学", "明确要按多少分钟"),
        ("每天1小时，8月25日休息", "补充例外日期的年份"),
    ],
)
async def test_ambiguous_availability_requests_targeted_clarification(
    value: str, question: str
) -> None:
    result = await AvailabilityInterpreter(FakeLLM()).interpret(value)

    assert result.status == "NEEDS_CLARIFICATION"
    assert question in str(result.question)


@pytest.mark.asyncio
async def test_explicit_correction_overrides_but_plain_conflict_clarifies() -> None:
    interpreter = AvailabilityInterpreter(FakeLLM())

    corrected = await interpreter.interpret("每天1小时\n改成每天2小时")
    conflict = await interpreter.interpret("每天1小时\n每天2小时")

    assert corrected.status == "COMPLETE"
    assert corrected.weekly_minutes == 840
    assert conflict.status == "NEEDS_CLARIFICATION"


@pytest.mark.asyncio
async def test_adjacent_turns_combine_scope_and_duration() -> None:
    result = await AvailabilityInterpreter(FakeLLM()).interpret("每天\n2小时")

    assert result.status == "COMPLETE"
    assert result.weekly_minutes == 840


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


def test_normalizer_rejects_all_excluded_windows_and_inconsistent_weekly_total() -> None:
    all_excluded = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            weekly_minutes=420,
            windows=tuple(
                AvailabilityWindowV1(day_of_week=day, available_minutes=60) for day in range(1, 8)
            ),
            excluded_days=tuple(range(1, 8)),
        )
    )
    inconsistent = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            weekly_minutes=120,
            windows=(AvailabilityWindowV1(day_of_week=1, available_minutes=60),),
        )
    )

    assert all_excluded.status == "NEEDS_CLARIFICATION"
    assert inconsistent.status == "NEEDS_CLARIFICATION"
    assert "不一致" in str(inconsistent.question)


def test_normalizer_sorts_non_overlapping_windows() -> None:
    result = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            windows=(
                AvailabilityWindowV1(
                    day_of_week=2,
                    available_minutes=30,
                    start_time="20:00:00",
                    end_time="20:30:00",
                ),
                AvailabilityWindowV1(
                    day_of_week=1,
                    available_minutes=60,
                    start_time="08:00:00",
                    end_time="09:00:00",
                ),
                AvailabilityWindowV1(
                    day_of_week=2,
                    available_minutes=30,
                    start_time="07:00:00",
                    end_time="07:30:00",
                ),
            ),
        )
    )

    assert [(item.day_of_week, str(item.start_time)) for item in result.windows] == [
        (1, "08:00:00"),
        (2, "07:00:00"),
        (2, "20:00:00"),
    ]


@pytest.mark.asyncio
async def test_fixed_availability_v2_benchmark() -> None:
    path = Path(__file__).parents[2] / "evals" / "availability-v2.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    interpreter = AvailabilityInterpreter(FakeLLM())

    for case in payload["cases"]:
        result = await interpreter.interpret(case["input"])
        assert result.status == case["status"], case["id"]
        assert result.weekly_minutes == case.get("weekly_minutes"), case["id"]
        assert [window.day_of_week for window in result.windows] == case.get("days", []), case["id"]
        if expected := case.get("question_contains"):
            assert expected in str(result.question), case["id"]


def test_summary_lists_custom_excluded_days() -> None:
    result = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            windows=(AvailabilityWindowV1(day_of_week=2, available_minutes=30),),
            excluded_days=(1,),
        )
    )
    assert result.summary == "周二 30 分钟，周一休息，每周共 30 分钟"


def test_summary_lists_explicit_excluded_dates() -> None:
    result = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            windows=(AvailabilityWindowV1(day_of_week=2, available_minutes=30),),
            excluded_dates=(date(2026, 8, 25),),
        )
    )
    assert result.summary == "周二 30 分钟，08月25日不安排学习，每周共 30 分钟"


def test_calendar_expands_explicit_rules_as_continuous_dates() -> None:
    availability = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            windows=tuple(
                AvailabilityWindowV1(day_of_week=day, available_minutes=60) for day in range(1, 6)
            ),
            excluded_days=(6, 7),
        )
    )

    days = expand_availability_calendar(
        availability,
        start_date=date(2026, 8, 17),
        end_date=date(2026, 8, 23),
    )

    assert [item.available_minutes for item in days] == [60, 60, 60, 60, 60, 0, 0]
    assert [item.source for item in days[-2:]] == ["EXCLUDED_WEEKDAY", "EXCLUDED_WEEKDAY"]


def test_calendar_evenly_splits_weekly_total_without_losing_remainder() -> None:
    availability = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(status="COMPLETE", weekly_minutes=360)
    )

    monday_first = expand_availability_calendar(
        availability,
        start_date=date(2026, 8, 17),
        end_date=date(2026, 8, 23),
    )
    cross_week = expand_availability_calendar(
        availability,
        start_date=date(2026, 8, 20),
        end_date=date(2026, 8, 26),
    )

    assert [item.available_minutes for item in monday_first] == [52, 52, 52, 51, 51, 51, 51]
    assert sum(item.available_minutes for item in monday_first) == 360
    assert sum(item.available_minutes for item in cross_week) == 360
    assert {item.source for item in monday_first} == {"WEEKLY_EVEN_SPLIT"}


def test_calendar_date_exception_clears_only_that_date() -> None:
    availability = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            weekly_minutes=360,
            excluded_days=(7,),
            excluded_dates=(date(2026, 8, 19),),
        )
    )

    days = expand_availability_calendar(
        availability,
        start_date=date(2026, 8, 17),
        end_date=date(2026, 8, 23),
    )

    assert [item.available_minutes for item in days] == [60, 60, 0, 60, 60, 60, 0]
    assert days[2].source == "EXCLUDED_DATE"
    assert days[-1].source == "EXCLUDED_WEEKDAY"


def test_calendar_sums_multiple_windows_and_stops_at_requested_end_date() -> None:
    availability = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            windows=(
                AvailabilityWindowV1(
                    day_of_week=1,
                    available_minutes=60,
                    start_time="08:00:00",
                    end_time="09:00:00",
                ),
                AvailabilityWindowV1(
                    day_of_week=1,
                    available_minutes=30,
                    start_time="20:00:00",
                    end_time="20:30:00",
                ),
            ),
        )
    )

    days = expand_availability_calendar(
        availability,
        start_date=date(2026, 8, 17),
        end_date=date(2026, 8, 19),
    )

    assert [(item.date, item.available_minutes, item.source) for item in days] == [
        (date(2026, 8, 17), 90, "EXPLICIT_RULE"),
        (date(2026, 8, 18), 0, "NO_RULE"),
        (date(2026, 8, 19), 0, "NO_RULE"),
    ]


def test_weekly_total_that_exceeds_eligible_daily_capacity_requires_clarification() -> None:
    result = AvailabilityInterpreter.normalize(
        AvailabilityInterpretationV1(
            status="COMPLETE",
            weekly_minutes=10080,
            excluded_days=(7,),
        )
    )

    assert result.status == "NEEDS_CLARIFICATION"
    assert "增加学习日或减少时长" in str(result.question)
