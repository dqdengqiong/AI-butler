from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.adapters.conversation_router import (
    ConversationRoute,
    ConversationRouteDecision,
    ConversationRouteRequest,
)
from ai_butler.api.schemas import (
    SendMessageRequest,
)
from ai_butler.domain.errors import ButlerError, conflict, not_found

from .context import ButlerContext
from .conversation_repository import ConversationRepository
from .shared import (
    NON_TERMINAL_RUN_SQL,
    _row,
)


class RoutingService:
    def __init__(self, context: ButlerContext, repository: ConversationRepository) -> None:
        self.database = context.database
        self.settings = context.settings
        self.conversation_router = context.conversation_router
        self._insert_automatic_conversation = repository._insert_automatic_conversation
        self._archive_or_discard_current = repository._archive_or_discard_current
        self._activate_conversation = repository._activate_conversation
        self._reserve_execution_slot = repository._reserve_execution_slot

    async def _preflight_conversation_route(
        self, user_id: UUID, request: SendMessageRequest
    ) -> tuple[UUID, datetime, ConversationRouteDecision] | None:
        """在事务外完成可能调用模型的语义判断。

        提交事务会重新校验会话更新时间；若预检期间上下文发生变化，则失败安全地
        延续当前会话。这里不写数据，也不把输入或模型输出写入日志。
        """

        if (
            request.context_policy != "AUTO"
            or request.target_conversation_id is not None
            or request.specialist_code is not None
            or not request.content.strip()
        ):
            return None
        async with self.database.connect() as connection:
            current = _row(
                await connection.execute(
                    text(
                        "SELECT id,title,updated_at,COALESCE(last_message_at,created_at) AS activity_at "
                        "FROM conversations WHERE user_id=:user_id AND status='CURRENT' "
                        "AND deleted_at IS NULL"
                    ),
                    {"user_id": user_id},
                )
            )
            if current is None:
                return None
            recent_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT content FROM messages WHERE conversation_id=:conversation_id "
                            "AND role IN ('USER','ASSISTANT') AND content<>'' "
                            "ORDER BY created_at DESC,id DESC LIMIT 6"
                        ),
                        {"conversation_id": current["id"]},
                    )
                )
                .scalars()
                .all()
            )
        activity_at = current["activity_at"]
        idle_seconds = max(0, int((datetime.now(UTC) - activity_at).total_seconds()))
        decision = await self.conversation_router.route(
            ConversationRouteRequest(
                current_title=str(current["title"]),
                recent_messages=tuple(reversed([str(item) for item in recent_rows])),
                user_input=request.content,
                idle_seconds=idle_seconds,
            )
        )
        return UUID(str(current["id"])), current["updated_at"], decision

    async def _resolve_message_conversation(
        self,
        connection: AsyncConnection,
        user_id: UUID,
        request: SendMessageRequest,
        now: datetime,
        preflight_route: tuple[UUID, datetime, ConversationRouteDecision] | None,
    ) -> tuple[dict[str, Any], dict[str, object]]:
        """在已锁定用户的事务中确定真实会话，并执行必要的场景切换。"""

        current = _row(
            await connection.execute(
                text(
                    "SELECT c.*,EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=c.id "
                    "AND m.role='USER') AS has_user_message FROM conversations c "
                    "WHERE c.user_id=:user_id AND c.status='CURRENT' AND c.deleted_at IS NULL "
                    "FOR UPDATE"
                ),
                {"user_id": user_id},
            )
        )
        if current is None:
            current = await self._insert_automatic_conversation(connection, user_id, None, now)

        if request.target_conversation_id is not None:
            target = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM conversations WHERE id=:id AND user_id=:user_id "
                        "AND deleted_at IS NULL FOR UPDATE"
                    ),
                    {"id": request.target_conversation_id, "user_id": user_id},
                )
            )
            if target is None:
                raise not_found()
            if target["id"] == current["id"]:
                return target, {"kind": "CONTINUED", "archived_conversation_id": None}
            archived = await self._activate_conversation(
                connection, current, target, now, "HISTORY_RESUME"
            )
            return target, {"kind": "RESUMED", "archived_conversation_id": archived}

        if request.specialist_code is not None:
            specialist = await self._resolve_specialist(
                connection, user_id, request.specialist_code
            )
            assert specialist is not None
            if current.get("specialist_user_agent_id") == specialist["user_agent_id"]:
                return current, {"kind": "CONTINUED", "archived_conversation_id": None}
            await self._reserve_execution_slot(connection, user_id, None, "CANCEL_OTHER")
            archived = await self._archive_or_discard_current(
                connection, current, now, "SPECIALIST_SWITCH"
            )
            created = await self._insert_automatic_conversation(
                connection, user_id, specialist, now
            )
            return created, {"kind": "CREATED", "archived_conversation_id": archived}

        if request.context_policy == "CONTINUE_CURRENT":
            return current, {"kind": "CONTINUED", "archived_conversation_id": None}

        if request.context_policy == "ARCHIVE_AND_START":
            archived = await self._archive_or_discard_current(
                connection, current, now, "TOPIC_SWITCH"
            )
            created = await self._insert_automatic_conversation(connection, user_id, None, now)
            return created, {"kind": "CREATED", "archived_conversation_id": archived}

        active = _row(
            await connection.execute(
                text(
                    f"SELECT id,status FROM agent_runs WHERE conversation_id=:conversation_id "  # noqa: S608
                    f"AND status IN ({NON_TERMINAL_RUN_SQL}) FOR UPDATE"
                ),
                {"conversation_id": current["id"]},
            )
        )
        if not current.get("has_user_message"):
            return current, {"kind": "CREATED", "archived_conversation_id": None}
        if preflight_route is None:
            return current, {"kind": "CONTINUED", "archived_conversation_id": None}
        expected_id, expected_updated_at, decision = preflight_route
        if expected_id != current["id"] or expected_updated_at != current["updated_at"]:
            return current, {"kind": "CONTINUED", "archived_conversation_id": None}
        if decision.route == ConversationRoute.CONTINUE:
            return current, {"kind": "CONTINUED", "archived_conversation_id": None}
        if (
            decision.route == ConversationRoute.AMBIGUOUS
            or decision.confidence < self.settings.conversation_topic_confidence
            or active is not None
        ):
            raise ButlerError(
                "TOPIC_SWITCH_CONFIRMATION_REQUIRED",
                "这看起来可能是一个新话题，请确认如何继续",
                409,
                False,
                {"reason_code": decision.reason_code},
            )
        archived = await self._archive_or_discard_current(connection, current, now, "TOPIC_SWITCH")
        created = await self._insert_automatic_conversation(connection, user_id, None, now)
        return created, {"kind": "CREATED", "archived_conversation_id": archived}

    async def _resolve_specialist(
        self, connection: AsyncConnection, user_id: UUID, specialist_code: str | None
    ) -> dict[str, Any] | None:
        """将公开 Agent code 解析为当前用户实例；未开放入口失败关闭。"""

        if specialist_code is None:
            return None
        row = _row(
            await connection.execute(
                text(
                    "SELECT ua.id AS user_agent_id,ad.code,ad.name,ad.catalog_status,"
                    "ad.catalog_metadata FROM agent_definitions ad "
                    "LEFT JOIN user_agents ua ON ua.agent_definition_id=ad.id "
                    "AND ua.user_id=:user_id AND ua.status='ACTIVE' "
                    "WHERE ad.code=:code ORDER BY ad.version DESC LIMIT 1"
                ),
                {"user_id": user_id, "code": specialist_code},
            )
        )
        if row is None or row["catalog_status"] != "AVAILABLE" or row["user_agent_id"] is None:
            raise conflict("AGENT_NOT_AVAILABLE", "该专业助理尚未开放")
        metadata = row["catalog_metadata"] if isinstance(row["catalog_metadata"], dict) else {}
        row["icon"] = str(metadata.get("icon", "AI"))
        row["welcome_message"] = str(metadata.get("welcome_message", ""))
        return row
