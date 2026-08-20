"""可复用的计划要求收集与确定性滚动窗口排期工具。"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, timedelta

from pydantic import ValidationError

from ai_butler.agent.availability import (
    AvailabilityInterpretationV1,
    AvailabilityInterpreter,
    expand_availability_calendar,
)
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
# 只判断当前回复是否意图补充或修改学习时间；模型仍负责理解具体语义。
AVAILABILITY_INPUT_PATTERN = re.compile(
    r"小时|分钟|每天|每日|天天|每周|工作日|平日|周末|"
    r"(?:周|星期)[一二三四五六日天]|改成|调整为"
)


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


class PlanRequirementCollector:
    """从聊天和服务端 workflow slots 合并提取完整计划要求。"""

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
        existing_slots: dict[str, object] | None = None,
    ) -> ToolResultV1:
        recent_user = [
            item.removeprefix("USER: ") for item in recent_messages if item.startswith("USER:")
        ]
        combined = "\n".join([*recent_user[-5:], current_input]).strip()
        existing_slots = existing_slots or {}
        stored_fields = existing_slots.get("scenario_fields")
        scenario_fields: dict[str, str] = {
            **scenario.default_fields,
            **(
                {str(key): str(value) for key, value in stored_fields.items()}
                if isinstance(stored_fields, dict)
                else {}
            ),
        }
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
        stored_target = existing_slots.get("target_date")
        target_date = explicit or (
            date.fromisoformat(str(stored_target)) if stored_target else None
        )
        period_source = str(existing_slots.get("period_source") or "EXPLICIT_DATE")
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

        stored_availability: AvailabilityInterpretationV1 | None = None
        stored_availability_value = existing_slots.get("availability")
        if stored_availability_value is not None:
            try:
                candidate = AvailabilityInterpretationV1.model_validate(stored_availability_value)
                if candidate.status == "COMPLETE":
                    stored_availability = candidate
            except (TypeError, ValidationError):
                # Workflow slots 是服务端快照，但可能来自旧版本；无效值不能进入计划事实。
                stored_availability = None
        current_mentions_availability = bool(AVAILABILITY_INPUT_PATTERN.search(current_input))
        if stored_availability is not None and not current_mentions_availability:
            interpretation = stored_availability
        elif current_mentions_availability:
            # 完整的新时间安排应覆盖近期消息中的旧值；只有“2 小时”这类单独回复
            # 仍不完整时，才回退到合并上下文以补齐上一轮的“每天”等范围。
            current_interpretation = await self._availability.interpret(
                current_input,
                run_id=run_id,  # type: ignore[arg-type]
            )
            interpretation = (
                current_interpretation
                if current_interpretation.status == "COMPLETE"
                else await self._availability.interpret(combined, run_id=run_id)  # type: ignore[arg-type]
            )
        else:
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
            missing.append(
                (interpretation.question or "每天或每周可以投入多少学习时间").rstrip("。？")
            )
        if missing:
            slots: dict[str, object] = {
                "scenario_code": scenario.code,
                "scenario_fields": scenario_fields,
                "target_date": target_date.isoformat() if target_date else None,
                "period_source": period_source,
                "missing": list(dict.fromkeys(missing)),
            }
            if interpretation.status == "COMPLETE":
                slots["availability"] = interpretation.model_dump(mode="json")
            return ToolResultV1(
                status="NEEDS_CLARIFICATION",
                data=slots,
                clarification=(
                    "为了生成计划预览，请一次告诉我：" + "；".join(dict.fromkeys(missing)) + "。"
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
    """按规范化每日容量、频率和稳定顺序物化一个日期窗口。

    预览与 Scheduler 都通过 ``expand_availability_calendar`` 解释可用时间；
    此处只施加计划任务使用的 85% 安全负荷，避免展示容量与排期容量各自推导。
    """

    del revision_ref
    availability_candidate = AvailabilityInterpretationV1.model_validate(
        {"status": "COMPLETE", **availability}
    )
    normalized = AvailabilityInterpreter.normalize(availability_candidate)
    if normalized.status != "COMPLETE" or not normalized.weekly_minutes:
        raise ValueError(normalized.question or "availability must be complete")
    daily_availability = expand_availability_calendar(
        normalized,
        start_date=window_start,
        end_date=window_end,
    )
    dates = [item.date for item in daily_availability if item.available_minutes > 0]
    remaining = {
        item.date: int(item.available_minutes * 0.85)
        for item in daily_availability
        if item.available_minutes > 0
    }
    weekly_minutes = normalized.weekly_minutes
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
