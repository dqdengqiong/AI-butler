from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any
from uuid import UUID, uuid5

from ai_butler.api.schemas import (
    SendMessageRequest,
)
from ai_butler.domain.errors import ButlerError

AGENT_NAMESPACE = UUID("d2542f33-9752-4d8a-bbdc-c77ecf2591d4")
BUTLER_DEFINITION_ID = uuid5(AGENT_NAMESPACE, "BUTLER:1")
CIVIL_DEFINITION_ID = uuid5(AGENT_NAMESPACE, "CIVIL_SERVICE_EXAM:1")
IELTS_DEFINITION_ID = uuid5(AGENT_NAMESPACE, "IELTS:1")
JOB_SEARCH_DEFINITION_ID = uuid5(AGENT_NAMESPACE, "JOB_SEARCH:1")
PUBLIC_SOURCE_ID = uuid5(AGENT_NAMESPACE, "SYNTHETIC_PUBLIC_SOURCE")
PUBLIC_CHUNK_ID = uuid5(AGENT_NAMESPACE, "SYNTHETIC_PUBLIC_SOURCE:0")
PLAN_PATTERN = re.compile(r"国考|省考|公务员|行测|申论|备考|计划")
PLAN_ACTION_PATTERN = re.compile(r"制定|生成|安排|调整|减少|增加|计划")
# 这里只做“是否值得进入结构化提取”的宽松检测；具体分钟与日期仍由 LLM 候选和
# AvailabilityInterpreter 的确定性规则共同完成，不能把正则命中当成业务事实。
TIME_PATTERN = re.compile(r"\d+\s*个?\s*(?:小时|分钟)|每天|每周|工作日|周末|周[一二三四五六日天]")
SEARCH_PATTERN = re.compile(
    r"政策|公告|报名|考试时间|岗位|大纲|资料|教材|联网|搜索|查询|最新|今年|202\d"
)
WEB_FORCE_PATTERN = re.compile(r"政策|公告|报名|考试时间|岗位|大纲|联网|搜索|查询|最新|今年|202\d")
PRIVATE_SEARCH_PATTERN = re.compile(r"我的资料|附件|文件|文档")
NON_TERMINAL_RUN_SQL = "'QUEUED','RUNNING','FAILED_RETRYABLE','CANCEL_REQUESTED'"
EXECUTING_RUN_SQL = "'QUEUED','RUNNING','CANCEL_REQUESTED'"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _content_hash(value: object) -> str:
    """对 JSON 兼容对象生成与字典键顺序无关的内容哈希。"""

    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _row(result: Any) -> dict[str, Any] | None:
    mapping = result.mappings().first()
    return dict(mapping) if mapping is not None else None


def _encode_cursor(*values: object) -> str:
    payload = _json([str(value) for value in values]).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str, expected_parts: int) -> list[str]:
    """解析客户端不透明游标；格式或字段数错误统一映射为安全业务错误。"""

    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ButlerError("INVALID_CURSOR", "分页游标无效", 400) from exc
    if (
        not isinstance(value, list)
        or len(value) != expected_parts
        or not all(isinstance(item, str) for item in value)
    ):
        raise ButlerError("INVALID_CURSOR", "分页游标无效", 400)
    return value


def _message_request_hash(
    request: SendMessageRequest, structured_input: dict[str, object] | None = None
) -> str:
    """计算会话内消息幂等哈希，附件按展示位置排序后进入摘要。"""

    canonical = {
        "content": request.content.strip(),
        "attachments": sorted(
            ((item.position, str(item.file_id)) for item in request.attachments),
            key=lambda item: item[0],
        ),
        "structured_input": structured_input or {},
    }
    return _content_hash(canonical)
