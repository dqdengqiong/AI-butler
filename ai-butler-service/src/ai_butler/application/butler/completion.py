from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.domain.errors import ButlerError

from .context import ButlerContext
from .events import EventService
from .shared import (
    _json,
    _row,
)


class CompletionService:
    def __init__(self, context: ButlerContext, events: EventService) -> None:
        self.database = context.database
        self.settings = context.settings
        self._append_event = events._append_event

    async def _complete_run(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        response: str,
        *,
        cards: list[dict[str, object]] | None = None,
    ) -> None:
        structured = {"cards": cards or []}
        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,"
                "structured_content=CAST(:structured AS jsonb),updated_at=now() WHERE id=:id"
            ),
            {
                "content": response,
                "structured": _json(structured),
                "id": run["response_message_id"],
            },
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='SUCCEEDED',updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "message.completed",
            {
                "message_id": str(run["response_message_id"]),
                "content": response,
                "cards": cards or [],
            },
            run["attempt"],
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "run.completed",
            {"status": "SUCCEEDED"},
            run["attempt"],
        )
        await self._maybe_archive_segment(connection, run)

    async def _fail_run(self, run_id: UUID, error: ButlerError) -> None:
        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                )
            )
            if run is None:
                return
            # 新建会话可以在 Worker 进行外部调用时立即取消旧 run；异常路径也
            # 必须尊重取消事实，不能把 CANCELLED 覆盖成失败并重新暴露旧回复。
            if run["status"] in {"CANCELLED", "CANCEL_REQUESTED"}:
                return
            status = "FAILED_RETRYABLE" if error.retryable else "FAILED_FINAL"
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status=:status,error_code=:code,updated_at=now() WHERE id=:id"
                ),
                {"status": status, "code": error.code, "id": run_id},
            )
            await connection.execute(
                text("UPDATE messages SET status='FAILED',updated_at=now() WHERE id=:id"),
                {"id": run["response_message_id"]},
            )
            await self._append_event(
                connection,
                run_id,
                run["user_id"],
                "error",
                {"code": error.code, "message": error.message, "retryable": error.retryable},
                run["attempt"],
            )

    async def _maybe_archive_segment(
        self, connection: AsyncConnection, run: dict[str, Any]
    ) -> None:
        total_chars = int(
            (
                await connection.execute(
                    text(
                        "SELECT COALESCE(SUM(length(content)),0) FROM messages WHERE segment_id=:segment"
                    ),
                    {"segment": run["segment_id"]},
                )
            ).scalar_one()
        )
        estimated = max(1, total_chars // 2)
        await connection.execute(
            text("UPDATE conversation_segments SET estimated_tokens=:tokens WHERE id=:id"),
            {"tokens": estimated, "id": run["segment_id"]},
        )
        hard = int(self.settings.context_window_tokens * self.settings.context_hard_limit_ratio)
        soft = int(self.settings.context_window_tokens * self.settings.context_soft_limit_ratio)
        if estimated >= soft:
            await connection.execute(
                text(
                    "INSERT INTO conversation_summaries(id,conversation_id,segment_id,summary_type,version,content,"
                    "source_message_count,token_count) VALUES(:id,:conversation,:segment,'INCREMENTAL',1,"
                    "CAST(:content AS jsonb),(SELECT COUNT(*) FROM messages WHERE segment_id=:segment),:tokens) "
                    "ON CONFLICT(conversation_id,segment_id,summary_type,version) DO UPDATE SET content=EXCLUDED.content,"
                    "source_message_count=EXCLUDED.source_message_count,token_count=EXCLUDED.token_count"
                ),
                {
                    "id": uuid4(),
                    "conversation": run["conversation_id"],
                    "segment": run["segment_id"],
                    "content": _json(
                        {"summary": "验证版确定性摘要", "source_segment_id": str(run["segment_id"])}
                    ),
                    "tokens": min(1500, estimated // 10),
                },
            )
        if estimated < hard:
            return
        conversation = _row(
            await connection.execute(
                text("SELECT * FROM conversations WHERE id=:id FOR UPDATE"),
                {"id": run["conversation_id"]},
            )
        )
        if conversation is None or conversation["active_segment_id"] != run["segment_id"]:
            return
        new_segment = uuid4()
        new_sequence = conversation["context_version"] + 1
        await connection.execute(
            text(
                "UPDATE conversation_segments SET status='ARCHIVED',archived_at=now() WHERE id=:id"
            ),
            {"id": run["segment_id"]},
        )
        await connection.execute(
            text(
                "INSERT INTO conversation_segments(id,conversation_id,user_id,sequence,thread_id,status) "
                "VALUES(:id,:conversation,:user_id,:sequence,:thread,'ACTIVE')"
            ),
            {
                "id": new_segment,
                "conversation": run["conversation_id"],
                "user_id": run["user_id"],
                "sequence": new_sequence,
                "thread": f"thread-{uuid4()}",
            },
        )
        await connection.execute(
            text(
                "UPDATE conversations SET active_segment_id=:segment,context_version=:version,updated_at=now() WHERE id=:id"
            ),
            {"segment": new_segment, "version": new_sequence, "id": run["conversation_id"]},
        )
