from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.domain.errors import ButlerError

from .events import EventService
from .shared import (
    EXECUTING_RUN_SQL,
    _row,
)


class ConversationRepository:
    def __init__(self, events: EventService) -> None:
        self._append_event = events._append_event

    async def _insert_automatic_conversation(
        self,
        connection: AsyncConnection,
        user_id: UUID,
        specialist: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        """创建首条消息即将写入的场景，不持久化欢迎消息或空历史。"""

        conversation_id = uuid4()
        segment_id = uuid4()
        title = f"{specialist['name']}助理" if specialist else "新的对话"
        await connection.execute(
            text(
                "INSERT INTO conversations(id,user_id,user_agent_id,active_segment_id,"
                "client_conversation_id,title,status,specialist_user_agent_id,created_at,updated_at) "
                "VALUES(:id,:user_id,:butler,NULL,:client_id,:title,'CURRENT',:specialist,:now,:now)"
            ),
            {
                "id": conversation_id,
                "user_id": user_id,
                "butler": uuid5(user_id, "BUTLER"),
                "client_id": conversation_id,
                "title": title,
                "specialist": specialist["user_agent_id"] if specialist else None,
                "now": now,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO conversation_segments(id,conversation_id,user_id,sequence,thread_id,status) "
                "VALUES(:id,:conversation,:user_id,1,:thread,'ACTIVE')"
            ),
            {
                "id": segment_id,
                "conversation": conversation_id,
                "user_id": user_id,
                "thread": f"thread-{uuid4()}",
            },
        )
        row = _row(
            await connection.execute(
                text(
                    "UPDATE conversations SET active_segment_id=:segment WHERE id=:id RETURNING *"
                ),
                {"segment": segment_id, "id": conversation_id},
            )
        )
        if row is None:
            raise RuntimeError("conversation update returned no row")
        return row

    async def _archive_or_discard_current(
        self,
        connection: AsyncConnection,
        current: dict[str, Any],
        now: datetime,
        reason: str,
    ) -> UUID | None:
        has_user_message = current.get("has_user_message")
        if has_user_message is None:
            has_user_message = bool(
                (
                    await connection.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM messages WHERE conversation_id=:id "
                            "AND role='USER')"
                        ),
                        {"id": current["id"]},
                    )
                ).scalar_one()
            )
        if not has_user_message:
            await connection.execute(
                text(
                    "UPDATE conversations SET deleted_at=:now,updated_at=:now "
                    "WHERE id=:id AND status='CURRENT'"
                ),
                {"id": current["id"], "now": now},
            )
            return None
        await connection.execute(
            text(
                "UPDATE conversations SET status='ARCHIVED',archived_at=:now,archive_reason=:reason,"
                "updated_at=:now WHERE id=:id AND status='CURRENT'"
            ),
            {"id": current["id"], "now": now, "reason": reason},
        )
        return UUID(str(current["id"]))

    async def _activate_conversation(
        self,
        connection: AsyncConnection,
        current: dict[str, Any],
        target: dict[str, Any],
        now: datetime,
        reason: str,
    ) -> UUID | None:
        archived = await self._archive_or_discard_current(connection, current, now, reason)
        await connection.execute(
            text(
                "UPDATE conversations SET status='CURRENT',archived_at=NULL,archive_reason=NULL,"
                "updated_at=:now WHERE id=:id"
            ),
            {"id": target["id"], "now": now},
        )
        target["status"] = "CURRENT"
        target["archived_at"] = None
        target["archive_reason"] = None
        target["updated_at"] = now
        return archived

    async def _reserve_execution_slot(
        self,
        connection: AsyncConnection,
        user_id: UUID,
        target_run_id: UUID | None,
        execution_policy: str,
    ) -> None:
        executing = _row(
            await connection.execute(
                text(
                    f"SELECT id,conversation_id,status,attempt FROM agent_runs "  # noqa: S608
                    f"WHERE user_id=:user_id AND status IN ({EXECUTING_RUN_SQL}) FOR UPDATE"
                ),
                {"user_id": user_id},
            )
        )
        if executing is None or (
            target_run_id is not None and UUID(str(executing["id"])) == target_run_id
        ):
            return
        if execution_policy != "CANCEL_OTHER":
            raise ButlerError(
                "OTHER_CONVERSATION_RUNNING",
                "另一个话题仍在处理中，请确认是否停止后切换",
                409,
            )
        await self._cancel_run_row(connection, user_id, executing, "CONVERSATION_SWITCH")

    async def _cancel_run_row(
        self,
        connection: AsyncConnection,
        user_id: UUID,
        run: dict[str, Any],
        reason: str,
    ) -> None:
        await connection.execute(
            text(
                "UPDATE agent_runs SET status='CANCELLED',cancel_requested_at=now(),updated_at=now() "
                "WHERE id=:id"
            ),
            {"id": run["id"]},
        )
        await connection.execute(
            text(
                "UPDATE messages SET status='CANCELLED',updated_at=now() "
                "WHERE agent_run_id=:run_id AND role='ASSISTANT' "
                "AND status IN ('PENDING','STREAMING')"
            ),
            {"run_id": run["id"]},
        )
        await self._append_event(
            connection,
            UUID(str(run["id"])),
            user_id,
            "run.cancelled",
            {"reason": reason},
            int(run["attempt"]),
        )
