from __future__ import annotations

from datetime import date
from itertools import pairwise

import pytest
from pydantic import ValidationError

from ai_butler.agent.availability import AvailabilityInterpretationV1
from ai_butler.agent.contracts import PlanScopeV1
from ai_butler.agent.plan_scope import (
    extract_explicit_target_date,
    quick_plan_period_options,
    stage_windows,
    target_date_for_weeks,
)


def _scope(start: date, target: date) -> PlanScopeV1:
    return PlanScopeV1(
        objective_summary="准备省考",
        availability=AvailabilityInterpretationV1(
            status="COMPLETE", weekly_minutes=300, summary="每周共 5 小时"
        ),
        start_date=start,
        target_date=target,
        period_source="CUSTOM_DATE",
    )


@pytest.mark.parametrize(
    ("weeks", "expected"),
    [
        (4, date(2026, 9, 14)),
        (8, date(2026, 10, 12)),
        (12, date(2026, 11, 9)),
    ],
)
def test_quick_period_uses_inclusive_week_range(weeks: int, expected: date) -> None:
    assert target_date_for_weeks(date(2026, 8, 18), weeks) == expected


def test_quick_period_options_are_server_snapshots() -> None:
    options = quick_plan_period_options(date(2026, 8, 18))
    assert [item["id"] for item in options] == [
        "period-4-weeks",
        "period-8-weeks",
        "period-12-weeks",
    ]
    assert options[0]["target_date"] == "2026-09-14"


@pytest.mark.parametrize("value", ["2026-10-31", "2026/10/31", "2026年10月31日"])
def test_custom_period_requires_and_accepts_full_year(value: str) -> None:
    assert extract_explicit_target_date(value, date(2026, 8, 18)) == date(2026, 10, 31)


@pytest.mark.parametrize("value", ["10月31日", "2026-08-18", "2025-12-31", "2026-02-30"])
def test_custom_period_rejects_missing_year_non_future_and_invalid_dates(value: str) -> None:
    assert extract_explicit_target_date(value, date(2026, 8, 18)) is None


def test_scope_rejects_non_future_target() -> None:
    with pytest.raises(ValidationError, match="later than start"):
        _scope(date(2026, 8, 18), date(2026, 8, 18))


def test_stage_windows_cover_scope_contiguously() -> None:
    windows = stage_windows(_scope(date(2026, 8, 18), date(2026, 11, 9)))
    assert len(windows) == 3
    assert windows[0][1] == date(2026, 8, 18)
    assert windows[-1][2] == date(2026, 11, 9)
    assert all(
        current[1].toordinal() == previous[2].toordinal() + 1
        for previous, current in pairwise(windows)
    )
