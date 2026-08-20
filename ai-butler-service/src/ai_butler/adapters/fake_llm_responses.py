"""本地 Fake LLM 的确定性结构化响应。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import TypedDict


class _FakeAvailabilityRule(TypedDict):
    """Fake 提取器使用的规则形状，与 availability-v2 测试契约保持一致。"""

    days: list[int]
    available_minutes: int | None
    start_time: str | None
    end_time: str | None


def fake_research_response(prompt: str) -> str:
    """使用第一条合成证据生成可通过引用 Gate 的离线回答。"""

    try:
        payload = json.loads(prompt)
        evidence = payload.get("evidence", [])
        first = evidence[0] if isinstance(evidence, list) and evidence else {}
        reference = str(first.get("ref") or "")
        content = str(first.get("content") or "")
        source_type = str(first.get("source_type") or "")
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        reference, content, source_type = "", "", ""
    if not reference or not content:
        return '{"schema_version":"1.0","segments":[]}'
    warnings = (
        ["合成离线来源仅用于验收，不代表真实考试公告。"] if source_type == "KNOWLEDGE" else []
    )
    return json.dumps(
        {
            "schema_version": "1.0",
            "segments": [{"text": content[:800], "evidence_refs": [reference]}],
            "warnings": warnings,
        },
        ensure_ascii=False,
    )


_DURATION_NUMBER = r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十]+)"
_NEGATIVE_TIME = re.compile(r"不学习|不学|休息|不安排")
_DAY_NUMBER = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}


def _fake_number(value: str) -> float:
    """解析测试语料使用的常见中文数字，不承担生产业务判断。"""

    if value[0].isdigit():
        return float(value)
    normalized = value.replace("两", "二")
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if normalized == "十":
        return 10
    if "十" in normalized:
        left, right = normalized.split("十", 1)
        return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return float(digits[normalized])


def _fake_duration(value: str) -> int | None:
    """把唯一的小时/分钟表达换算为分钟；区间返回 ``None``。"""

    range_pattern = (
        rf"{_DURATION_NUMBER}\s*[~～—-]|"
        rf"{_DURATION_NUMBER}\s*(?:到|至)\s*{_DURATION_NUMBER}\s*(?:小时|分钟)"
    )
    if re.search(range_pattern, value):
        return None
    if re.search(r"(?:一个|一|1个?)半小时", value):
        return 90
    if "半小时" in value:
        return 30
    hours = re.search(rf"({_DURATION_NUMBER})\s*个?\s*小时", value)
    minutes = re.search(rf"({_DURATION_NUMBER})\s*分钟", value)
    total = 0.0
    if hours:
        total += _fake_number(hours.group(1)) * 60
    if minutes:
        total += _fake_number(minutes.group(1))
    return int(total) if total > 0 and total.is_integer() else None


def _fake_days(value: str) -> tuple[int, ...]:
    """识别 Fake LLM 基准集中的常见星期范围。"""

    if re.search(r"工作日|平日|周一\s*(?:到|至|[-—])\s*周五", value):
        return (1, 2, 3, 4, 5)
    if "周末" in value:
        return (6, 7)
    if re.search(r"每天|每日|天天", value):
        return (1, 2, 3, 4, 5, 6, 7)
    days = tuple(
        dict.fromkeys(
            _DAY_NUMBER[item] for item in re.findall(r"(?:周|星期)([一二三四五六日天])", value)
        )
    )
    return days


def _fake_time_range(value: str) -> tuple[str, str] | None:
    """解析带“点”或冒号的日内时段；跨午夜交给澄清分支。"""

    match = re.search(
        r"(?P<qualifier>早上|上午|中午|下午|晚上)?\s*"
        r"(?P<start>\d{1,2})(?::(?P<start_colon>\d{2})|点(?P<start_point>半|\d{1,2})?)"
        r"\s*(?:到|至|[-—])\s*"
        r"(?P<end_qualifier>早上|上午|中午|下午|晚上)?\s*"
        r"(?P<end>\d{1,2})(?::(?P<end_colon>\d{2})|点(?P<end_point>半|\d{1,2})?)",
        value,
    )
    if match is None:
        return None

    def clock(hour_value: str, minute_value: str | None, qualifier: str | None) -> int:
        hour = int(hour_value)
        minute = 30 if minute_value == "半" else int(minute_value or 0)
        if qualifier in {"下午", "晚上"} and hour < 12:
            hour += 12
        return hour * 60 + minute

    qualifier = match.group("qualifier")
    start = clock(
        match.group("start"), match.group("start_colon") or match.group("start_point"), qualifier
    )
    end = clock(
        match.group("end"),
        match.group("end_colon") or match.group("end_point"),
        match.group("end_qualifier") or qualifier,
    )
    if start >= end or end > 24 * 60:
        return None
    return f"{start // 60:02d}:{start % 60:02d}:00", f"{end // 60:02d}:{end % 60:02d}:00"


def fake_availability_response(prompt: str) -> str:
    """为本地和测试环境模拟 v2 时间提取，不调用外部模型或保存用户原文。"""

    marker = "USER_INPUT:\n"
    try:
        user_input = json.loads(prompt.rsplit(marker, 1)[1])
    except (IndexError, json.JSONDecodeError):
        user_input = ""

    excluded: set[int] = set()
    excluded_dates: set[str] = set()
    clauses = [item.strip() for item in re.split(r"[，。；;\n]", user_input) if item.strip()]
    for clause in clauses:
        if not _NEGATIVE_TIME.search(clause):
            continue
        excluded.update(_fake_days(clause))
        date_match = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?", clause)
        if date_match:
            excluded_dates.add(
                f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
            )
        elif re.search(r"(?<!\d)\d{1,2}月\d{1,2}日", clause):
            return json.dumps(
                {
                    "schema_version": "2.0",
                    "status": "NEEDS_CLARIFICATION",
                    "weekly_minutes": None,
                    "rules": [],
                    "excluded_days": sorted(excluded),
                    "excluded_dates": sorted(excluded_dates),
                    "question": "请补充例外日期的年份。",
                },
                ensure_ascii=False,
            )

    rules: list[_FakeAvailabilityRule] = []
    conflict = False
    for clause in clauses:
        if _NEGATIVE_TIME.search(clause):
            continue
        days = _fake_days(clause)
        if not days:
            continue
        time_range = _fake_time_range(clause)
        minutes = _fake_duration(clause)
        if time_range is None and minutes is None:
            continue
        candidate: _FakeAvailabilityRule = {
            "days": list(days),
            "available_minutes": minutes,
            "start_time": time_range[0] if time_range else None,
            "end_time": time_range[1] if time_range else None,
        }
        overlapping = [item for item in rules if set(item["days"]) & set(days)]
        if overlapping and re.search(r"改成|调整为|不是.+是", clause):
            rules = [item for item in rules if not set(item["days"]) & set(days)]
        elif overlapping and any(item != candidate for item in overlapping):
            conflict = True
        if candidate not in rules:
            rules.append(candidate)

    # 支持用户先回答“每天”，下一轮只补“2小时”的自然对话。
    if not rules:
        days = _fake_days(user_input)
        time_range = _fake_time_range(user_input)
        minutes = _fake_duration(user_input)
        if days and (time_range is not None or minutes is not None):
            rules.append(
                {
                    "days": list(days),
                    "available_minutes": minutes,
                    "start_time": time_range[0] if time_range else None,
                    "end_time": time_range[1] if time_range else None,
                }
            )

    weekly_clause = next((item for item in clauses if "每周" in item), "")
    weekly_minutes = _fake_duration(weekly_clause) if weekly_clause else None
    vague = bool(re.search(r"有空就学|学一会", user_input))
    ranged = bool(
        re.search(
            rf"{_DURATION_NUMBER}\s*(?:[~～—-]|到|至)\s*{_DURATION_NUMBER}\s*(?:小时|分钟)",
            user_input,
        )
    )
    cross_midnight = bool(
        re.search(r"晚上[^，。；\n]*(?:到|至|[-—])[^，。；\n]*(?:凌晨|早上)", user_input)
    )
    if conflict or vague or ranged or cross_midnight:
        question = "学习时长存在多种理解，请明确要按多少分钟计算。"
        status = "NEEDS_CLARIFICATION"
    elif not rules and weekly_minutes is None:
        scope = _fake_days(user_input)
        question = (
            f"{('每天' if scope == tuple(range(1, 8)) else '这些天')}可以学习多少小时或分钟？"
            if scope
            else "请说明每天或每周可以投入多少小时或分钟。"
        )
        status = "NEEDS_CLARIFICATION"
    else:
        question, status = None, "COMPLETE"

    covered = {day for rule in rules for day in rule["days"]}
    if covered == set(range(1, 6)):
        excluded.update((6, 7))
    elif covered == {6, 7}:
        excluded.update(range(1, 6))
    result = {
        "schema_version": "2.0",
        "status": status,
        "weekly_minutes": weekly_minutes,
        "rules": rules,
        "excluded_days": sorted(excluded),
        "excluded_dates": sorted(excluded_dates),
        "question": question,
    }
    return json.dumps(result, ensure_ascii=False)


def fake_intent_response(prompt: str) -> str:
    """为离线测试选择稳定业务流程，不产生实体标识或副作用授权。"""

    try:
        payload = json.loads(prompt)
        value = str(payload.get("user_input") or "").strip()
        attachment_count = int(payload.get("attachment_count") or 0)
        recent_messages = payload.get("recent_messages", [])
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        value, attachment_count, recent_messages = "", 0, []
    answering_plan_question = any(
        "为了生成计划预览" in str(item)
        for item in recent_messages
        if isinstance(recent_messages, list)
    )
    if not value:
        intent, confidence, question = "CLARIFY", 0.4, "请告诉我你希望我协助处理什么。"
    elif re.search(r"(?:记住|忘记|暂停记忆|恢复记忆|纠正)", value):
        intent, confidence, question = "MEMORY", 0.99, None
    elif re.search(r"(?:规划|安排).*(?:今天|今日)|(?:今天|今日).*(?:规划|安排)", value):
        intent, confidence, question = "DAILY_PLANNING", 0.95, None
    elif re.search(r"(?:复盘|回顾).*(?:计划|进度)|(?:计划|进度).*(?:复盘|回顾)", value):
        intent, confidence, question = "PLAN_REVIEW", 0.95, None
    elif re.search(r"(?:查找|搜索|查询).*(?:资料|来源|信息)", value):
        intent, confidence, question = "RESEARCH", 0.95, None
    elif re.search(r"(?:调整|修改|降低|增加).*(?:计划|任务)|(?:计划|任务).*(?:调整|修改)", value):
        intent, confidence, question = "PLAN_ADJUST", 0.95, None
    elif answering_plan_question or re.search(
        r"(?:制定|生成|安排).*(?:国考|省考|公务员|行测|申论|备考|计划)", value
    ):
        intent, confidence, question = "PLAN_CREATE", 0.95, None
    elif re.search(
        r"(?:没完成|完成了|跳过|太难|太多).*(?:任务|学习)"
        r"|(?:任务|学习).*(?:没完成|完成了|跳过|太难|太多)",
        value,
    ):
        intent, confidence, question = "TASK_FEEDBACK", 0.9, None
    elif re.search(r"(?:国考|省考|公务员|行测|申论|考公)", value):
        intent, confidence, question = "CIVIL_QA", 0.92, None
    elif re.search(r"(?:诊断|处方|股票买卖|保证收益|成功率预测)", value):
        intent, confidence, question = "UNSUPPORTED", 0.9, None
    else:
        intent, confidence, question = "GENERAL_CHAT", 0.9, None
    needs_web = (
        intent == "RESEARCH"
        or bool(
            re.search(
                r"(?:联网|搜索|查询|最新|今天|当前|价格|政策|公告|报名|考试时间|202\d)", value
            )
        )
        or intent in {"PLAN_CREATE", "PLAN_ADJUST"}
    )
    context_needs: list[str] = []
    if intent in {"PLAN_CREATE", "PLAN_ADJUST"}:
        context_needs.append("PLAN_REQUIREMENTS")
    if intent in {"DAILY_PLANNING", "PLAN_REVIEW", "TASK_FEEDBACK", "PLAN_ADJUST"}:
        context_needs.append("PLAN_CONTEXT")
    if intent in {"DAILY_PLANNING", "PLAN_REVIEW", "TASK_FEEDBACK"}:
        context_needs.append("TASK_CONTEXT")
    if needs_web:
        context_needs.append("PUBLIC_KNOWLEDGE")
    if attachment_count > 0 or bool(re.search(r"(?:我的资料|附件|文件|文档)", value)):
        context_needs.append("PRIVATE_KNOWLEDGE")
    if intent == "MEMORY":
        context_needs.append("MEMORY_COMMAND")
    return json.dumps(
        {
            "schema_version": "1.0",
            "intent": intent,
            "confidence": confidence,
            "context_needs": context_needs,
            "clarifying_question": question,
        },
        ensure_ascii=False,
    )


def fake_feedback_response(prompt: str) -> str:
    """为离线任务反馈分流提供稳定结构化结果。"""

    try:
        payload = json.loads(prompt)
        value = str(payload.get("user_input") or "")
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        value = ""
    if re.search(r"(?:太多|太难|降低|调整|改计划)", value):
        action, summary = "REPLAN", "用户希望调整当前计划的负荷或难度。"
    elif value.strip():
        action, summary = "RESPOND", "用户提供了任务完成反馈。"
    else:
        action, summary = "CLARIFY", "任务反馈信息不足。"
    return json.dumps(
        {
            "schema_version": "1.0",
            "action": action,
            "confidence": 0.9 if value.strip() else 0.4,
            "summary": summary,
            "clarifying_question": "请说明是哪项任务以及遇到了什么问题。"
            if action == "CLARIFY"
            else None,
        },
        ensure_ascii=False,
    )


def fake_planner_response(prompt: str) -> str:
    """生成可通过确定性 Review 的紧凑建议，供本地闭环与回归测试使用。"""

    try:
        payload = json.loads(prompt)
        weekly_minutes = max(1, int(payload.get("weekly_minutes") or 210))
        objective = str(payload.get("objective") or "公务员备考")[:200]
        availability = payload.get("availability")
        availability_windows = (
            availability.get("windows", []) if isinstance(availability, dict) else []
        )
        max_daily_minutes = max(
            (
                int(item.get("available_minutes") or 0)
                for item in availability_windows
                if isinstance(item, dict)
            ),
            default=max(1, weekly_minutes // 3),
        )
        windows = payload["stage_windows"]
        if not isinstance(windows, list) or not windows:
            raise ValueError("missing stage windows")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        weekly_minutes, objective = 210, "公务员备考"
        max_daily_minutes = weekly_minutes // 3
        windows = [{"stage_key": "stage_1"}, {"stage_key": "stage_2"}]
    result: dict[str, object] = {
        "schema_version": "2.0",
        "status": "READY",
        "title": "公务员备考计划",
        "objective_summary": objective,
        "assumptions": [],
        "stages": [
            {
                "stage_key": str(window["stage_key"]),
                "name": f"阶段 {index}",
                "objective": "完成本阶段训练并复盘薄弱项。",
                "task_templates": [
                    {
                        "title": "训练与复盘",
                        "description": "完成训练并记录错误原因。",
                        "days_per_week": 3,
                        "expected_minutes": max(
                            1, min(weekly_minutes // 3, int(max_daily_minutes * 0.85))
                        ),
                        "priority": 2,
                        "claim_keys": [],
                    }
                ],
            }
            for index, window in enumerate(windows, 1)
        ],
        "question": None,
        "adjustment_options": [],
        "warnings": [],
    }
    return json.dumps(result, ensure_ascii=False)


def fake_executor_response(prompt: str) -> str:
    """从批准模板生成七日内稳定任务键，模拟模型排期候选。"""

    try:
        payload = json.loads(prompt)
        templates = payload.get("templates", [])
        availability = payload.get("availability", {})
        start = datetime.fromisoformat(str(payload["current_date"])).date()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        templates, availability, start = [], {}, datetime.now(UTC).date()
    windows = availability.get("windows", []) if isinstance(availability, dict) else []
    daily_capacity = {
        int(item["day_of_week"]): max(1, int(int(item["available_minutes"]) * 0.85))
        for item in windows
        if isinstance(item, dict)
    }
    candidate_dates = [start + timedelta(days=offset) for offset in range(7)]
    if daily_capacity:
        candidate_dates = [item for item in candidate_dates if item.isoweekday() in daily_capacity]
    drafts: list[dict[str, object]] = []
    for index, item in enumerate(templates if isinstance(templates, list) else []):
        if not isinstance(item, dict):
            continue
        template_key = str(item.get("template_key") or f"template-{index + 1}")
        stage_key = str(item.get("stage_key") or "stage")
        if not candidate_dates:
            continue
        scheduled = candidate_dates[index % len(candidate_dates)]
        expected = int(item.get("expected_minutes") or 30)
        if daily_capacity:
            expected = min(expected, daily_capacity[scheduled.isoweekday()])
        drafts.append(
            {
                "task_key": f"{stage_key}:{template_key}:{scheduled.isoformat()}",
                "stage_key": stage_key,
                "template_key": template_key,
                "scheduled_date": scheduled.isoformat(),
                "title": str(item.get("title") or "学习任务")[:200],
                "expected_minutes": expected,
                "priority": int(item.get("priority") or 3),
            }
        )
    return json.dumps(
        {"schema_version": "1.0", "task_drafts": drafts, "unscheduled": [], "warnings": []},
        ensure_ascii=False,
    )


def fake_response_text(_prompt: str) -> str:
    """提供不依赖用户原文的稳定通用回答，避免测试快照包含敏感输入。"""

    return "我已经理解你的问题，会结合当前会话上下文给出清晰、可执行的建议。"
