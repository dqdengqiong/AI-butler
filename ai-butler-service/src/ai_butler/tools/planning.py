"""可复用的计划要求收集与确定性滚动窗口排期工具。"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, timedelta

from ai_butler.agent.availability import AvailabilityInterpreter
from ai_butler.agent.plan_scope import extract_explicit_target_date

from .contracts import (
    PlanningScenarioSpec,
    PlanRequirementsV1,
    ScheduledTaskV1,
    ToolResultV1,
)


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, (int, str)):
        return int(value)
    return default


CIVIL_SERVICE_SCENARIO = PlanningScenarioSpec(
    code="CIVIL_SERVICE_EXAM",
    label="考公",
    required_fields=("exam_type", "region", "target_year"),
    field_prompts={
        "exam_type": "国考还是省考",
        "region": "省考地区",
        "target_year": "目标考试年份",
    },
    retrieval_query_prefix="公务员考试备考",
)
PROVINCES = (
    "北京|天津|上海|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|"
    "湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾|内蒙古|广西|西藏|宁夏|新疆|香港|澳门"
)
DURATION_PATTERN = re.compile(r"(?P<value>\d{1,3})\s*(?P<unit>个月|月|周|天)")
YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?:年)?")
PROVINCE_PATTERN = re.compile(rf"({PROVINCES})(?:省|市|自治区|特别行政区)?")


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


class PlanRequirementCollector:
    """从普通聊天重新提取完整计划要求，不保存跨 run 草稿状态。"""

    def __init__(self, availability: AvailabilityInterpreter) -> None:
        self._availability = availability

    async def collect(
        self,
        *,
        current_input: str,
        recent_messages: tuple[str, ...],
        start_date: date,
        scenario: PlanningScenarioSpec = CIVIL_SERVICE_SCENARIO,
        run_id: object | None = None,
    ) -> ToolResultV1:
        recent_user = [
            item.removeprefix("USER: ") for item in recent_messages if item.startswith("USER:")
        ]
        combined = "\n".join([*recent_user[-5:], current_input]).strip()
        scenario_fields: dict[str, str] = dict(scenario.default_fields)
        if "国考" in combined:
            scenario_fields.update(exam_type="NATIONAL", region="全国")
        elif "省考" in combined:
            scenario_fields["exam_type"] = "PROVINCIAL"
            province = PROVINCE_PATTERN.search(combined)
            if province:
                scenario_fields["region"] = province.group(1)
        year = YEAR_PATTERN.search(combined)
        if year:
            scenario_fields["target_year"] = year.group(1)
        for field, pattern in scenario.field_patterns.items():
            match = re.search(pattern, combined)
            if match:
                scenario_fields[field] = match.group(1) if match.groups() else match.group(0)

        explicit = extract_explicit_target_date(combined, start_date)
        target_date = explicit
        period_source = "EXPLICIT_DATE"
        if target_date is None:
            duration = DURATION_PATTERN.search(combined)
            if duration:
                count = int(duration.group("value"))
                unit = duration.group("unit")
                if unit == "天":
                    target_date, period_source = start_date + timedelta(days=count), "RELATIVE_DAYS"
                elif unit == "周":
                    target_date = start_date + timedelta(weeks=count)
                    period_source = "RELATIVE_WEEKS"
                else:
                    target_date, period_source = _add_months(start_date, count), "RELATIVE_MONTHS"

        interpretation = await self._availability.interpret(combined, run_id=run_id)  # type: ignore[arg-type]
        missing: list[str] = []
        missing.extend(
            scenario.field_prompts.get(item, item)
            for item in scenario.required_fields
            if not scenario_fields.get(item)
        )
        if target_date is None:
            missing.append("备考到哪一天或准备几天、几周、几个月")
        if interpretation.status != "COMPLETE":
            missing.append("工作日和周末每天可投入的时间")
        if missing:
            return ToolResultV1(
                status="NEEDS_CLARIFICATION",
                clarification=(
                    "为了生成计划预览，请一次告诉我：" + "、".join(dict.fromkeys(missing)) + "。"
                ),
            )
        assert target_date is not None
        objective = " ".join(
            item.strip() for item in [*recent_user[-2:], current_input] if item.strip()
        )
        return ToolResultV1(
            status="COMPLETED",
            data=PlanRequirementsV1(
                scenario_code=scenario.code,
                objective_summary=objective[:4000],
                start_date=start_date,
                target_date=target_date,
                period_source=period_source,  # type: ignore[arg-type]
                availability=interpretation,
                scenario_fields=scenario_fields,
            ),
        )


def schedule_plan_window(
    *,
    revision_ref: str,
    templates: tuple[dict[str, object], ...],
    stages: tuple[dict[str, object], ...],
    availability: dict[str, object],
    window_start: date,
    window_end: date,
    existing: tuple[dict[str, object], ...] = (),
) -> tuple[tuple[ScheduledTaskV1, ...], tuple[dict[str, object], ...]]:
    """按容量、频率和稳定顺序物化一个日期窗口。"""

    del revision_ref
    windows = availability.get("windows", [])
    excluded_value = availability.get("excluded_days", [])
    excluded_items = excluded_value if isinstance(excluded_value, (list, tuple)) else ()
    excluded = {_as_int(value) for value in excluded_items}
    excluded_dates_value = availability.get("excluded_dates", [])
    excluded_date_items = (
        excluded_dates_value if isinstance(excluded_dates_value, (list, tuple)) else ()
    )
    excluded_dates = {
        value if isinstance(value, date) else date.fromisoformat(str(value))
        for value in excluded_date_items
    }
    capacity_by_weekday: dict[int, int] = defaultdict(int)
    for item in windows if isinstance(windows, list) else []:
        if isinstance(item, dict):
            capacity_by_weekday[_as_int(item["day_of_week"])] += _as_int(item["available_minutes"])
    weekly_minutes = _as_int(availability.get("weekly_minutes"))
    flexible_daily = max(1, weekly_minutes // 7) if weekly_minutes else 0
    dates: list[date] = []
    cursor = window_start
    while cursor <= window_end:
        if (
            cursor not in excluded_dates
            and cursor.isoweekday() not in excluded
            and (capacity_by_weekday or flexible_daily)
        ):
            if not capacity_by_weekday or cursor.isoweekday() in capacity_by_weekday:
                dates.append(cursor)
        cursor += timedelta(days=1)
    remaining = {
        day: int(capacity_by_weekday.get(day.isoweekday(), flexible_daily) * 0.85) for day in dates
    }
    weekly_used: dict[tuple[int, int], int] = defaultdict(int)
    occurrences: dict[tuple[str, tuple[int, int]], int] = defaultdict(int)
    existing_keys = {str(item.get("task_key")) for item in existing}
    for item in existing:
        scheduled = item.get("scheduled_date")
        if not isinstance(scheduled, date):
            continue
        week = scheduled.isocalendar()[:2]
        minutes = _as_int(item.get("expected_minutes"))
        weekly_used[week] += minutes
        occurrences[(str(item.get("template_key")), week)] += 1

    stage_by_key = {str(item.get("stage_key")): item for item in stages}
    ordered = sorted(
        templates,
        key=lambda item: (_as_int(item.get("priority"), 3), str(item.get("template_key"))),
    )
    tasks: list[ScheduledTaskV1] = []
    unscheduled: list[dict[str, object]] = []
    for template in ordered:
        template_key = str(template["template_key"])
        stage_key = str(template["stage_key"])
        stage = stage_by_key.get(stage_key, {})
        stage_start = date.fromisoformat(str(stage.get("start_date")))
        stage_end = date.fromisoformat(str(stage.get("end_date")))
        frequency = template.get("frequency")
        days_per_week = (
            _as_int(frequency.get("days_per_week"), 1) if isinstance(frequency, dict) else 1
        )
        minutes = _as_int(template["expected_minutes"])
        weeks = sorted({candidate.isocalendar()[:2] for candidate in dates})
        for week in weeks:
            needed = max(0, days_per_week - occurrences[(template_key, week)])
            candidates = [
                candidate
                for candidate in dates
                if candidate.isocalendar()[:2] == week
                and stage_start <= candidate <= stage_end
                and remaining[candidate] >= minutes
                and weekly_used[week] + minutes <= int(weekly_minutes * 0.85)
                and f"{template_key}:{candidate.isoformat()}" not in existing_keys
            ]
            candidates.sort(key=lambda candidate: (-remaining[candidate], candidate, template_key))
            for candidate in candidates[:needed]:
                task_key = f"{template_key}:{candidate.isoformat()}"
                tasks.append(
                    ScheduledTaskV1(
                        task_key=task_key,
                        stage_key=stage_key,
                        template_key=template_key,
                        title=str(template["title"]),
                        scheduled_date=candidate,
                        expected_minutes=minutes,
                        priority=_as_int(template.get("priority"), 3),
                    )
                )
                remaining[candidate] -= minutes
                weekly_used[week] += minutes
                occurrences[(template_key, week)] += 1
            if occurrences[(template_key, week)] < days_per_week:
                unscheduled.append(
                    {"template_key": template_key, "week": week, "reason": "CAPACITY"}
                )
    return tuple(tasks), tuple(unscheduled)
