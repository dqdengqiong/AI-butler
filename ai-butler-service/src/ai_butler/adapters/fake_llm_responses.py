"""本地 Fake LLM 的确定性结构化响应。"""

from __future__ import annotations

import json
import re


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
    number = re.search(r"(\d+)\s*个?\s*(小时|分钟)", user_input)
    minutes = (
        int(number.group(1)) * (60 if number and number.group(2) == "小时" else 1) if number else 0
    )
    days: tuple[int, ...] = ()
    excluded: tuple[int, ...] = ()
    if "工作日" in user_input:
        days, excluded = tuple(range(1, 6)), (6, 7)
    elif "周末" in user_input and not re.search(r"周末\s*(?:不|不再|不安排|休息)", user_input):
        days, excluded = (6, 7), tuple(range(1, 6))
    elif "每天" in user_input:
        days = tuple(range(1, 8))
    if re.search(r"周末.*(?:不学习|不学|休息|不安排)", user_input):
        excluded = (6, 7)
    if number and "每周" in user_input and not days:
        result = {
            "schema_version": "1.0",
            "status": "COMPLETE",
            "weekly_minutes": minutes,
            "windows": [],
            "excluded_days": [],
            "question": None,
        }
    elif number and days:
        result = {
            "schema_version": "1.0",
            "status": "COMPLETE",
            "weekly_minutes": None,
            "windows": [
                {
                    "day_of_week": day,
                    "available_minutes": minutes,
                    "start_time": None,
                    "end_time": None,
                }
                for day in days
            ],
            "excluded_days": list(excluded),
            "question": None,
        }
    else:
        result = {
            "schema_version": "1.0",
            "status": "NEEDS_CLARIFICATION",
            "weekly_minutes": None,
            "windows": [],
            "excluded_days": [],
            "question": "请说明每天或每周可以投入多少小时或分钟。",
        }
    return json.dumps(result, ensure_ascii=False)
