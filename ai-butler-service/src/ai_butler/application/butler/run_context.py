from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from ai_butler.agent.contracts import ContextBundleV1, ContextItemV1
from ai_butler.agent.evidence import estimate_tokens
from ai_butler.agent.runtime import ContextBudgetGuard
from ai_butler.domain.errors import ButlerError

from .shared import _row

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
    owner: Any, run_id: UUID, *, include_memories: bool = True
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
                        "SELECT role,content FROM messages WHERE conversation_id=:conversation "
                        "AND user_id=:user_id AND id<>:current AND role IN ('USER','ASSISTANT') "
                        "AND content<>'' ORDER BY created_at DESC,id DESC LIMIT 20"
                    ),
                    {
                        "conversation": run["conversation_id"],
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
                        "ORDER BY updated_at DESC LIMIT 20"
                    ),
                    {"user_id": run["user_id"]},
                )
            ).scalars()
        )
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
                        "SELECT summary_data FROM conversation_summaries WHERE conversation_id=:conversation "
                        "AND status='PUBLISHED' ORDER BY version DESC,created_at DESC LIMIT 3"
                    ),
                    {"conversation": run["conversation_id"]},
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
        str(value.get("summary"))
        for value in reversed(summary_rows)
        if isinstance(value, dict) and value.get("summary")
    )
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
                trust_level="SYSTEM_FACT",
                estimated_tokens=estimate_tokens(title),
            )
            for index, title in enumerate(plan_titles)
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
                trust_level="USER_CONTENT",
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
    compacted = ContextBudgetGuard(
        max(256, int(owner.settings.context_window_tokens * 0.85) - 1024)
    ).compact(bundle)
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
