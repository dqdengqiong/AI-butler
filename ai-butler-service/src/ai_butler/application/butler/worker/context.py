"""Agent Run 的预算内上下文组装。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text

from ai_butler.agent.contracts import ContextBundleV1, ContextItemV1
from ai_butler.agent.evidence import estimate_tokens
from ai_butler.agent.runtime import ContextBudgetGuard
from ai_butler.domain.errors import ButlerError

from ..shared import _json, _row

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunContext:
    run: dict[str, object]
    user_input: str
    request_data: dict[str, object]
    published_summaries: tuple[str, ...]
    recent_messages: tuple[str, ...]
    memories: tuple[str, ...]
    active_plan_titles: tuple[str, ...]
    attachment_count: int


async def build_run_context(
    owner: Any,
    run_id: UUID,
    *,
    include_memories: bool = True,
    task: Literal["ROUTER", "GENERAL", "PLANNING", "RESEARCH"] = "GENERAL",
) -> RunContext:
    """从受用户隔离的服务端事实构建预算内单轮上下文。"""

    async with owner.database.connect() as connection:
        run = _row(
            await connection.execute(
                text(
                    "SELECT r.*,s.thread_id FROM agent_runs r JOIN conversation_segments s "
                    "ON s.id=r.segment_id WHERE r.id=:id"
                ),
                {"id": run_id},
            )
        )
        if run is None:
            raise ButlerError("RUN_NOT_FOUND", "运行不存在", 404)
        message = _row(
            await connection.execute(
                text(
                    "SELECT content,structured_content FROM messages WHERE id=:id "
                    "AND user_id=:user_id"
                ),
                {"id": run["pending_message_id"], "user_id": run["user_id"]},
            )
        ) or {"content": "", "structured_content": {}}
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT role,content FROM messages WHERE segment_id=:segment "
                        "AND user_id=:user_id AND id<>:current AND role IN ('USER','ASSISTANT') "
                        "AND content<>'' ORDER BY created_at DESC,id DESC LIMIT 8"
                    ),
                    {
                        "segment": run["segment_id"],
                        "user_id": run["user_id"],
                        "current": run["pending_message_id"],
                    },
                )
            )
            .mappings()
            .all()
        )
        plan_titles = tuple(
            str(value)
            for value in (
                await connection.execute(
                    text(
                        "SELECT title FROM plans WHERE user_id=:user_id AND status='ACTIVE' "
                        "ORDER BY updated_at DESC LIMIT 8"
                    ),
                    {"user_id": run["user_id"]},
                )
            ).scalars()
        )
        profile_row = (
            _row(
                await connection.execute(
                    text(
                        "SELECT education_level,major,region_code,current_level FROM user_profiles "
                        "WHERE user_id=:user_id"
                    ),
                    {"user_id": run["user_id"]},
                )
            )
            or {}
        )
        profile_snapshot = (
            await connection.execute(
                text(
                    "SELECT profile_data FROM user_profile_snapshots WHERE user_id=:user_id "
                    "AND status='FRESH'"
                ),
                {"user_id": run["user_id"]},
            )
        ).scalar_one_or_none()
        attachment_count = int(
            (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM message_attachments ma JOIN messages m ON m.id=ma.message_id "
                        "WHERE m.id=:message AND m.user_id=:user_id"
                    ),
                    {"message": run["pending_message_id"], "user_id": run["user_id"]},
                )
            ).scalar_one()
        )
        summary_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT summary_data FROM conversation_summaries "
                        "WHERE conversation_id=:conversation AND status='PUBLISHED' AND "
                        "(summary_type='CUMULATIVE_HANDOFF' OR segment_id=:segment) "
                        "ORDER BY CASE WHEN summary_type='CUMULATIVE_HANDOFF' THEN 0 ELSE 1 END,"
                        "version DESC,created_at DESC LIMIT 4"
                    ),
                    {"conversation": run["conversation_id"], "segment": run["segment_id"]},
                )
            )
            .scalars()
            .all()
        )
    user_input = str(message.get("content") or "")
    request_data = message.get("structured_content")
    request_data = request_data if isinstance(request_data, dict) else {}
    memories: tuple[str, ...] = ()
    if include_memories:
        try:
            memories = await owner._memory.search(UUID(str(run["user_id"])), user_input)
        except Exception:
            logger.warning("long-term memory lookup unavailable", extra={"run_id": str(run_id)})
    summaries = tuple(
        _summary_text(value)
        for value in summary_rows
        if isinstance(value, dict) and _summary_text(value)
    )
    profile_facts = () if task == "ROUTER" else _profile_facts(profile_row, profile_snapshot)
    bundle = ContextBundleV1(
        user_id=UUID(str(run["user_id"])),
        run_id=run_id,
        thread_id=str(run["thread_id"]),
        current_input=ContextItemV1(
            ref="current-input",
            text=user_input,
            trust_level="USER_CONTENT",
            estimated_tokens=estimate_tokens(user_input),
        ),
        business_facts=tuple(
            ContextItemV1(
                ref=f"active-plan-{index}",
                text=title,
                trust_level="USER_DERIVED",
                estimated_tokens=estimate_tokens(title),
            )
            for index, title in enumerate(plan_titles)
        )
        + tuple(
            ContextItemV1(
                ref=f"user-profile-{index}",
                text=value,
                trust_level="USER_DERIVED",
                estimated_tokens=estimate_tokens(value),
            )
            for index, value in enumerate(profile_facts)
        ),
        summaries=tuple(
            ContextItemV1(
                ref=f"published-summary-{index}",
                text=value,
                trust_level="SYSTEM_FACT",
                estimated_tokens=estimate_tokens(value),
            )
            for index, value in enumerate(summaries)
        ),
        messages=tuple(
            ContextItemV1(
                ref=f"message-{index}",
                text=f"{row['role']}: {row['content']}",
                trust_level="USER_DERIVED",
                estimated_tokens=estimate_tokens(str(row["content"])),
            )
            for index, row in enumerate(reversed(rows))
        ),
        memories=tuple(
            ContextItemV1(
                ref=f"memory-{index}",
                text=value,
                trust_level="USER_CONTENT",
                estimated_tokens=estimate_tokens(value),
            )
            for index, value in enumerate(memories)
        ),
    )
    target, hard = _task_budget(owner.settings, task)
    compacted = ContextBudgetGuard(target, hard).compact(bundle)
    await _save_context_manifest(owner, run, bundle, compacted, task, target, hard)
    return RunContext(
        run=run,
        user_input=compacted.current_input.text,
        request_data=request_data,
        published_summaries=tuple(item.text for item in compacted.summaries),
        recent_messages=tuple(item.text for item in compacted.messages),
        memories=tuple(item.text for item in compacted.memories),
        active_plan_titles=plan_titles,
        attachment_count=attachment_count,
    )


def _task_budget(settings: Any, task: str) -> tuple[int, int]:
    prefix = {
        "ROUTER": "router",
        "GENERAL": "general",
        "PLANNING": "planning",
        "RESEARCH": "research",
    }[task]
    return (
        int(getattr(settings, f"{prefix}_context_target_tokens")),
        int(getattr(settings, f"{prefix}_context_hard_tokens")),
    )


def _summary_text(value: dict[str, object]) -> str:
    legacy = value.get("summary")
    if legacy:
        return str(legacy)
    parts: list[str] = []
    for label, key in (
        ("目标", "current_goal"),
        ("最近上下文", "recent_context"),
        ("未决问题", "open_questions"),
        ("决策", "decisions"),
    ):
        item = value.get(key)
        if isinstance(item, list):
            text_value = "；".join(str(entry) for entry in item if str(entry).strip())
        else:
            text_value = str(item or "").strip()
        if text_value:
            parts.append(f"{label}：{text_value}")
    return "\n".join(parts)


def _profile_facts(explicit: dict[str, object], snapshot: object) -> tuple[str, ...]:
    facts: list[str] = []
    for label, key in (
        ("学历", "education_level"),
        ("专业", "major"),
        ("地区", "region_code"),
        ("当前阶段", "current_level"),
    ):
        if explicit.get(key):
            facts.append(f"{label}：{explicit[key]}")
    if isinstance(snapshot, dict):
        for label, key in (
            ("偏好", "preferences"),
            ("习惯", "habits"),
            ("稳定约束", "constraints"),
            ("背景", "background"),
        ):
            values = snapshot.get(key)
            if isinstance(values, list) and values:
                facts.append(f"{label}：{'；'.join(str(value) for value in values[:4])}")
    return tuple(facts)


async def _save_context_manifest(
    owner: Any,
    run: dict[str, object],
    original: ContextBundleV1,
    compacted: ContextBundleV1,
    task: str,
    target: int,
    hard: int,
) -> None:
    selected = (
        compacted.current_input,
        *compacted.business_facts,
        *compacted.summaries,
        *compacted.messages,
        *compacted.memories,
        *compacted.evidence,
    )
    original_count = 1 + sum(
        len(items)
        for items in (
            original.business_facts,
            original.summaries,
            original.messages,
            original.memories,
            original.evidence,
        )
    )
    async with owner.database.transaction() as connection:
        await connection.execute(
            text(
                "INSERT INTO context_manifests(id,user_id,agent_run_id,segment_id,task_kind,"
                "target_tokens,hard_tokens,estimated_tokens,selected_refs,truncated) "
                "VALUES(:id,:user_id,:run_id,:segment_id,:task,:target,:hard,:estimated,"
                "CAST(:refs AS jsonb),:truncated)"
            ),
            {
                "id": uuid4(),
                "user_id": run["user_id"],
                "run_id": run["id"],
                "segment_id": run["segment_id"],
                "task": task,
                "target": target,
                "hard": hard,
                "estimated": sum(item.estimated_tokens for item in selected),
                "refs": _json([item.ref for item in selected]),
                "truncated": len(selected) < original_count,
            },
        )
