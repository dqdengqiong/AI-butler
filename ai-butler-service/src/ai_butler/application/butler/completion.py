from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.adapters.llm import ModelStreamEvent
from ai_butler.agent.evidence import estimate_tokens
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
                "id": run["pending_response_message_id"],
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
                "message_id": str(run["pending_response_message_id"]),
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

    async def _complete_validated_run(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        response: str,
        *,
        cards: list[dict[str, object]] | None = None,
    ) -> None:
        """对已完整校验的内容补齐可重放的 start/delta/completed 契约。"""

        await connection.execute(
            text("UPDATE messages SET status='STREAMING',content='' WHERE id=:id"),
            {"id": run["pending_response_message_id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "message.start",
            {"message_id": str(run["pending_response_message_id"])},
            run["attempt"],
        )
        for offset in range(0, len(response), 128):
            await self._append_event(
                connection,
                run["id"],
                run["user_id"],
                "message.delta",
                {"delta": response[offset : offset + 128]},
                run["attempt"],
            )
        await self._complete_run(connection, run, response, cards=cards)

    async def _stream_complete_run(
        self,
        run_id: UUID,
        stream: AsyncIterator[ModelStreamEvent],
    ) -> None:
        """以短事务持久化公开文本流，并用最终完整消息覆盖临时增量。

        模型网络等待期间不持有数据库事务。Delta 只进入可重放事件，Assistant
        正文在完整流成功后一次写入；主备切换的 ``reset`` 会清空本次内存缓冲并
        通知客户端，因而不会把两个模型的部分回答拼接。
        """

        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                )
            )
            if run is None or run["status"] != "RUNNING":
                return
            await connection.execute(
                text(
                    "UPDATE messages SET status='STREAMING',content='',updated_at=now() "
                    "WHERE id=:id"
                ),
                {"id": run["pending_response_message_id"]},
            )
            await self._append_event(
                connection,
                run_id,
                run["user_id"],
                "message.start",
                {"message_id": str(run["pending_response_message_id"])},
                run["attempt"],
            )

        complete_text = ""
        pending_delta = ""
        last_flush = time.monotonic()
        async for event in stream:
            if event.reset:
                complete_text = ""
                pending_delta = ""
                await self._append_stream_event(run_id, "message.reset", {})
                continue
            if event.delta:
                complete_text += event.delta
                pending_delta += event.delta
                if len(pending_delta) >= 128 or time.monotonic() - last_flush >= 0.1:
                    await self._append_stream_event(
                        run_id, "message.delta", {"delta": pending_delta}
                    )
                    pending_delta = ""
                    last_flush = time.monotonic()
        if pending_delta:
            await self._append_stream_event(run_id, "message.delta", {"delta": pending_delta})
        if not complete_text:
            raise ButlerError("MODEL_EMPTY_RESPONSE", "模型没有生成可用回答", 502)

        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                )
            )
            if run is None or run["status"] != "RUNNING":
                return
            await self._complete_run(connection, run, complete_text)

    async def _append_stream_event(
        self, run_id: UUID, event_type: str, payload: dict[str, object]
    ) -> None:
        """写入单个公开流事件，并在每个增量边界重新检查取消状态。"""

        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT user_id,status,attempt FROM agent_runs WHERE id=:id FOR UPDATE"),
                    {"id": run_id},
                )
            )
            if run is None or run["status"] != "RUNNING":
                raise ButlerError("RUN_CANCELLED", "运行已取消", 409)
            await self._append_event(
                connection,
                run_id,
                run["user_id"],
                event_type,
                payload,
                run["attempt"],
            )

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
                {"id": run["pending_response_message_id"]},
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
        contents = (
            (
                await connection.execute(
                    text(
                        "SELECT content FROM messages WHERE segment_id=:segment ORDER BY created_at,id"
                    ),
                    {"segment": run["segment_id"]},
                )
            )
            .scalars()
            .all()
        )
        estimated = sum(estimate_tokens(str(content)) for content in contents)
        estimated += 256  # system facts, card metadata and model output reserve
        await connection.execute(
            text("UPDATE conversation_segments SET estimated_context_tokens=:tokens WHERE id=:id"),
            {"tokens": estimated, "id": run["segment_id"]},
        )
        hard = int(self.settings.context_window_tokens * self.settings.context_hard_limit_ratio)
        soft = int(self.settings.context_window_tokens * self.settings.context_soft_limit_ratio)
        if estimated >= soft:
            await connection.execute(
                text(
                    "INSERT INTO conversation_summaries(id,conversation_id,segment_id,summary_type,version,"
                    "summary_data,source_hash,prompt_version,token_count) VALUES(:id,:conversation,:segment,"
                    "'INCREMENTAL',1,CAST(:content AS jsonb),:source_hash,'summary-v1',:tokens) "
                    "ON CONFLICT(conversation_id,summary_type,version) DO UPDATE SET "
                    "summary_data=EXCLUDED.summary_data,source_hash=EXCLUDED.source_hash,"
                    "token_count=EXCLUDED.token_count"
                ),
                {
                    "id": uuid4(),
                    "conversation": run["conversation_id"],
                    "segment": run["segment_id"],
                    "content": _json(
                        {"summary": "验证版确定性摘要", "source_segment_id": str(run["segment_id"])}
                    ),
                    "source_hash": hashlib.sha256(
                        f"{run['segment_id']}:{estimated}".encode()
                    ).hexdigest(),
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
        final_summary_id = uuid4()
        final_version = int(conversation["context_version"])
        final_hash = hashlib.sha256(f"final:{run['segment_id']}:{estimated}".encode()).hexdigest()
        await connection.execute(
            text(
                "INSERT INTO conversation_summaries(id,conversation_id,segment_id,summary_type,version,"
                "summary_data,source_hash,prompt_version,token_count) VALUES(:id,:conversation,:segment,"
                "'SEGMENT_FINAL',:version,CAST(:content AS jsonb),:source_hash,'summary-v1',:tokens) "
                "ON CONFLICT(conversation_id,summary_type,version) DO NOTHING"
            ),
            {
                "id": final_summary_id,
                "conversation": run["conversation_id"],
                "segment": run["segment_id"],
                "version": final_version,
                "content": _json(
                    {
                        "summary": "验证版终态交接摘要",
                        "memory_refs": [],
                        "source_segment_id": str(run["segment_id"]),
                    }
                ),
                "source_hash": final_hash,
                "tokens": min(1500, max(1, estimated // 10)),
            },
        )
        final_summary_id = UUID(
            str(
                (
                    await connection.execute(
                        text(
                            "SELECT id FROM conversation_summaries WHERE conversation_id=:conversation "
                            "AND summary_type='SEGMENT_FINAL' AND version=:version"
                        ),
                        {"conversation": run["conversation_id"], "version": final_version},
                    )
                ).scalar_one()
            )
        )
        handoff_id = uuid4()
        handoff_version = final_version
        handoff_hash = hashlib.sha256(
            f"handoff:{run['conversation_id']}:{handoff_version}:{final_summary_id}".encode()
        ).hexdigest()
        await connection.execute(
            text(
                "INSERT INTO conversation_summaries(id,conversation_id,segment_id,summary_type,version,"
                "summary_data,source_hash,prompt_version,token_count) VALUES(:id,:conversation,:segment,"
                "'CUMULATIVE_HANDOFF',:version,CAST(:content AS jsonb),:source_hash,'summary-v1',:tokens)"
            ),
            {
                "id": handoff_id,
                "conversation": run["conversation_id"],
                "segment": run["segment_id"],
                "version": handoff_version,
                "content": _json(
                    {
                        "summary": "累计交接摘要",
                        "memory_refs": [],
                        "final_summary_id": str(final_summary_id),
                    }
                ),
                "source_hash": handoff_hash,
                "tokens": min(1800, max(1, estimated // 8)),
            },
        )
        new_segment = uuid4()
        new_sequence = conversation["context_version"] + 1
        await connection.execute(
            text(
                "UPDATE conversation_segments SET status='ARCHIVING',end_message_id=:message,"
                "final_summary_id=:summary WHERE id=:id"
            ),
            {
                "id": run["segment_id"],
                "message": run["pending_response_message_id"],
                "summary": final_summary_id,
            },
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
                "UPDATE conversations SET active_segment_id=:segment,context_version=:version,"
                "latest_handoff_summary_id=:handoff,updated_at=now() WHERE id=:id"
            ),
            {
                "segment": new_segment,
                "version": new_sequence,
                "handoff": handoff_id,
                "id": run["conversation_id"],
            },
        )
        await connection.execute(
            text(
                "UPDATE conversation_segments SET status='ARCHIVED',archived_at=now() WHERE id=:id"
            ),
            {"id": run["segment_id"]},
        )
