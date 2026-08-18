"""计划周期解析与服务端阶段窗口计算。"""

from __future__ import annotations

import re
from datetime import date, timedelta

from ai_butler.agent.contracts import PlanScopeV1

EXPLICIT_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>20\d{2})\s*(?:年|[-/])\s*(?P<month>\d{1,2})\s*"
    r"(?:月|[-/])\s*(?P<day>\d{1,2})\s*日?"
)


def extract_explicit_target_date(value: str, start_date: date) -> date | None:
    """解析带四位年份的未来日期；无年份或无效日期一律不猜测。"""

    match = EXPLICIT_DATE_PATTERN.search(value)
    if match is None:
        return None
    try:
        parsed = date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None
    return parsed if parsed > start_date else None


def target_date_for_weeks(start_date: date, weeks: int) -> date:
    """把快捷周期转换成包含首尾两天的自然日范围。"""

    if weeks not in {4, 8, 12}:
        raise ValueError("unsupported quick plan period")
    return start_date + timedelta(days=weeks * 7 - 1)


def quick_plan_period_options(start_date: date) -> tuple[dict[str, object], ...]:
    """返回服务端生成的周期选项，客户端只能回传选项 ID。"""

    return tuple(
        {
            "id": f"period-{weeks}-weeks",
            "label": f"{weeks} 周",
            "weeks": weeks,
            "target_date": target_date_for_weeks(start_date, weeks).isoformat(),
        }
        for weeks in (4, 8, 12)
    )


def stage_windows(scope: PlanScopeV1) -> tuple[tuple[str, date, date], ...]:
    """按总周期均匀切分 2–4 个连续阶段，余数优先分配给前序阶段。"""

    total_days = (scope.target_date - scope.start_date).days + 1
    count = 2 if total_days <= 56 else 3 if total_days <= 112 else 4
    base, remainder = divmod(total_days, count)
    windows: list[tuple[str, date, date]] = []
    cursor = scope.start_date
    for index in range(count):
        length = base + (1 if index < remainder else 0)
        end = cursor + timedelta(days=length - 1)
        windows.append((f"stage_{index + 1}", cursor, end))
        cursor = end + timedelta(days=1)
    return tuple(windows)
