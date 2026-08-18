"""本地 Fake LLM 的确定性结构化响应。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta


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


def fake_availability_response(prompt: str) -> str:
    """为本地和测试环境模拟时间提取，不调用外部模型或保存用户原文。"""

    marker = "USER_INPUT:\n"
    try:
        user_input = json.loads(prompt.rsplit(marker, 1)[1])
    except (IndexError, json.JSONDecodeError):
        user_input = ""

    def duration(match: re.Match[str] | None) -> int:
        if match is None:
            return 0
        return int(match.group(1)) * (60 if match.group(2) == "小时" else 1)

    weekday = re.search(r"工作日[^，。；\n]*?(\d+)\s*个?\s*(小时|分钟)", user_input)
    weekend = re.search(r"周末[^，。；\n]*?(\d+)\s*个?\s*(小时|分钟)", user_input)
    general = re.search(r"(\d+)\s*个?\s*(小时|分钟)", user_input)
    total = re.search(r"每周[^，。；\n]*?(\d+)\s*个?\s*(小时|分钟)", user_input)
    windows: list[dict[str, object]] = []
    excluded: tuple[int, ...] = ()
    if weekday:
        windows.extend(
            {
                "day_of_week": day,
                "available_minutes": duration(weekday),
                "start_time": None,
                "end_time": None,
            }
            for day in range(1, 6)
        )
    if weekend and not re.search(r"周末.*(?:不学习|不学|休息|不安排)", user_input):
        windows.extend(
            {
                "day_of_week": day,
                "available_minutes": duration(weekend),
                "start_time": None,
                "end_time": None,
            }
            for day in (6, 7)
        )
    elif re.search(r"周末.*(?:不学习|不学|休息|不安排)", user_input):
        excluded = (6, 7)
    if weekday and not weekend:
        excluded = (6, 7)
    elif weekend and not weekday:
        excluded = (1, 2, 3, 4, 5)
    if not windows and general and "每天" in user_input:
        windows.extend(
            {
                "day_of_week": day,
                "available_minutes": duration(general),
                "start_time": None,
                "end_time": None,
            }
            for day in range(1, 8)
        )
    excluded_dates = sorted(
        set(
            re.findall(
                r"(20\d{2}-\d{2}-\d{2})(?=[^。；;]{0,12}(?:不学习|不学|休息|不安排))",
                user_input,
            )
        )
    )
    if total and not windows:
        result = {
            "schema_version": "1.0",
            "status": "COMPLETE",
            "weekly_minutes": duration(total),
            "windows": [],
            "excluded_days": [],
            "excluded_dates": excluded_dates,
            "question": None,
        }
    elif windows:
        result = {
            "schema_version": "1.0",
            "status": "COMPLETE",
            "weekly_minutes": None,
            "windows": windows,
            "excluded_days": list(excluded),
            "excluded_dates": excluded_dates,
            "question": None,
        }
    else:
        result = {
            "schema_version": "1.0",
            "status": "NEEDS_CLARIFICATION",
            "weekly_minutes": None,
            "windows": [],
            "excluded_days": [],
            "excluded_dates": excluded_dates,
            "question": "请说明每天或每周可以投入多少小时或分钟。",
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
