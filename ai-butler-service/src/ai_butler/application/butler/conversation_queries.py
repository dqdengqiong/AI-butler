from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.domain.errors import ButlerError, conflict, not_found

from .context import ButlerContext
from .shared import (
    NON_TERMINAL_RUN_SQL,
    _decode_cursor,
    _encode_cursor,
    _row,
)


class ConversationQueryService:
    def __init__(self, context: ButlerContext) -> None:
        self.database = context.database

    async def list_conversations(
        self, user_id: UUID, limit: int = 30, cursor: str | None = None
    ) -> dict[str, object]:
        """按当前优先、最近活动倒序列出用户可见的非空历史会话。"""

        parameters: dict[str, object] = {"user_id": user_id, "limit": limit + 1}
        cursor_clause = ""
        if cursor:
            rank, timestamp, conversation_id = _decode_cursor(cursor, 3)
            try:
                parameters.update(
                    {
                        "cursor_rank": int(rank),
                        "cursor_time": datetime.fromisoformat(timestamp),
                        "cursor_id": UUID(conversation_id),
                    }
                )
            except ValueError as exc:
                raise ButlerError("INVALID_CURSOR", "分页游标无效", 400) from exc
            cursor_clause = (
                "AND (CASE WHEN c.status='CURRENT' THEN 0 ELSE 1 END > :cursor_rank OR "
                "(CASE WHEN c.status='CURRENT' THEN 0 ELSE 1 END = :cursor_rank AND "
                "(COALESCE(c.last_message_at,c.created_at) < :cursor_time OR "
                "(COALESCE(c.last_message_at,c.created_at) = :cursor_time "
                "AND c.id < :cursor_id)))) "
            )
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT c.*,ad.code AS specialist_code,ad.name AS specialist_name,"  # noqa: S608
                            "ad.catalog_metadata AS specialist_metadata,r.id AS active_run_id,"
                            "r.status AS active_run_status,lm.content AS last_message_content,"
                            "lm.created_at AS last_message_created_at "
                            "FROM conversations c "
                            "LEFT JOIN user_agents sua ON sua.id=c.specialist_user_agent_id "
                            "LEFT JOIN agent_definitions ad ON ad.id=sua.agent_definition_id "
                            f"LEFT JOIN agent_runs r ON r.conversation_id=c.id AND r.status IN ({NON_TERMINAL_RUN_SQL}) "
                            "LEFT JOIN LATERAL (SELECT content,created_at FROM messages "
                            "WHERE conversation_id=c.id AND role IN ('USER','ASSISTANT') "
                            "ORDER BY created_at DESC,id DESC LIMIT 1) lm ON true "
                            "WHERE c.user_id=:user_id AND c.deleted_at IS NULL "
                            "AND EXISTS (SELECT 1 FROM messages um "
                            "WHERE um.conversation_id=c.id AND um.role='USER') "
                            f"{cursor_clause}"
                            "ORDER BY CASE WHEN c.status='CURRENT' THEN 0 ELSE 1 END,"
                            "COALESCE(c.last_message_at,c.created_at) DESC,c.id DESC LIMIT :limit"
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        items = [self._conversation_response(dict(row)) for row in visible_rows]
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = _encode_cursor(
                0 if last["status"] == "CURRENT" else 1,
                last["last_message_at"] or last["created_at"],
                last["id"],
            )
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    async def get_conversation(self, user_id: UUID, conversation_id: UUID) -> dict[str, object]:
        """读取一个归属当前用户的会话，跨用户访问按不存在处理。"""

        async with self.database.connect() as connection:
            row = await self._conversation_row(connection, user_id, conversation_id)
        if row is None:
            raise not_found()
        return self._conversation_response(row)

    async def delete_conversation(self, user_id: UUID, conversation_id: UUID) -> None:
        """软删除当前用户的已归档会话。

        删除对同一用户幂等；已删除记录保留在数据库中，但所有公共会话读取与
        消息入口都会将其视为不存在。CURRENT 会话必须先通过新建流程归档，
        避免客户端失去唯一可继续的会话。
        """

        async with self.database.transaction() as connection:
            conversation = _row(
                await connection.execute(
                    text(
                        "SELECT id,status,deleted_at FROM conversations "
                        "WHERE id=:conversation_id AND user_id=:user_id FOR UPDATE"
                    ),
                    {"conversation_id": conversation_id, "user_id": user_id},
                )
            )
            if conversation is None:
                raise not_found()
            if conversation["deleted_at"] is not None:
                return
            if conversation["status"] != "ARCHIVED":
                raise conflict(
                    "CURRENT_CONVERSATION_DELETE_FORBIDDEN",
                    "当前话题不能删除；开始其他话题后可在历史中删除",
                )
            await connection.execute(
                text(
                    "UPDATE conversations SET deleted_at=now(),updated_at=now() "
                    "WHERE id=:conversation_id"
                ),
                {"conversation_id": conversation_id},
            )

    async def list_messages(
        self,
        user_id: UUID,
        conversation_id: UUID,
        limit: int = 30,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """按会话分页读取消息；响应保持正序以供聊天时间线直接追加。"""

        parameters: dict[str, object] = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "limit": limit + 1,
        }
        cursor_clause = ""
        if cursor:
            timestamp, message_id = _decode_cursor(cursor, 2)
            try:
                parameters.update(
                    {
                        "cursor_time": datetime.fromisoformat(timestamp),
                        "cursor_id": UUID(message_id),
                    }
                )
            except ValueError as exc:
                raise ButlerError("INVALID_CURSOR", "分页游标无效", 400) from exc
            cursor_clause = "AND (created_at,id) < (:cursor_time,:cursor_id) "
        async with self.database.connect() as connection:
            owned = _row(
                await connection.execute(
                    text(
                        "SELECT id FROM conversations WHERE id=:conversation_id AND user_id=:user_id "
                        "AND deleted_at IS NULL"
                    ),
                    parameters,
                )
            )
            if owned is None:
                raise not_found()
            result = await connection.execute(
                text(
                    "SELECT id,role,status,content,structured_content AS cards,agent_run_id,created_at "  # noqa: S608
                    "FROM messages WHERE user_id=:user_id AND conversation_id=:conversation_id "
                    "AND role IN ('USER','ASSISTANT','SYSTEM_EVENT') "
                    f"{cursor_clause}ORDER BY created_at DESC,id DESC LIMIT :limit"
                ),
                parameters,
            )
            rows = [dict(row) for row in result.mappings().all()]
            await self._hydrate_approval_cards(connection, user_id, rows)
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        items = list(reversed(visible_rows))
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = _encode_cursor(last["created_at"], last["id"])
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    async def _conversation_row(
        self, connection: AsyncConnection, user_id: UUID, conversation_id: UUID
    ) -> dict[str, Any] | None:
        return _row(
            await connection.execute(
                text(
                    "SELECT c.*,ad.code AS specialist_code,ad.name AS specialist_name,"  # noqa: S608
                    "ad.catalog_metadata AS specialist_metadata,r.id AS active_run_id,"
                    "r.status AS active_run_status,lm.content AS last_message_content,"
                    "lm.created_at AS last_message_created_at FROM conversations c "
                    "LEFT JOIN user_agents ua ON ua.id=c.specialist_user_agent_id "
                    "LEFT JOIN agent_definitions ad ON ad.id=ua.agent_definition_id "
                    f"LEFT JOIN agent_runs r ON r.conversation_id=c.id AND r.status IN ({NON_TERMINAL_RUN_SQL}) "
                    "LEFT JOIN LATERAL (SELECT content,created_at FROM messages "
                    "WHERE conversation_id=c.id AND role IN ('USER','ASSISTANT') "
                    "ORDER BY created_at DESC,id DESC LIMIT 1) lm ON true "
                    "WHERE c.id=:conversation_id AND c.user_id=:user_id "
                    "AND c.deleted_at IS NULL"
                ),
                {"conversation_id": conversation_id, "user_id": user_id},
            )
        )

    async def _hydrate_approval_cards(
        self,
        connection: AsyncConnection,
        user_id: UUID,
        messages: list[dict[str, Any]],
    ) -> None:
        """用审批事实覆盖消息快照中的可变版本与草案摘要。

        PlanCard 是历史消息的一部分，但 EDIT 会在同一 approval 上生成新版本。
        读取时必须投影当前版本，既修复旧数据，也避免客户端刷新后继续提交旧版本。
        """

        approval_ids: set[UUID] = set()
        for message in messages:
            structured = message.get("cards")
            cards = structured.get("cards", []) if isinstance(structured, dict) else []
            for card in cards:
                if not isinstance(card, dict) or card.get("card_type") != "PlanCard":
                    continue
                refs = card.get("entity_refs")
                if not isinstance(refs, dict) or not refs.get("approval_id"):
                    continue
                try:
                    approval_ids.add(UUID(str(refs["approval_id"])))
                except ValueError:
                    continue
        if not approval_ids:
            return
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT DISTINCT ON (a.id) a.id,a.approval_version,a.status,"
                        "pr.objective_summary,pr.weekly_minutes FROM approval_decisions a "
                        "JOIN approval_decision_items ai ON ai.approval_id=a.id "
                        "JOIN plan_revisions pr ON pr.id=ai.plan_revision_id "
                        "WHERE a.user_id=:user_id AND a.id=ANY(:approval_ids) "
                        "ORDER BY a.id,ai.id"
                    ),
                    {"user_id": user_id, "approval_ids": list(approval_ids)},
                )
            )
            .mappings()
            .all()
        )
        current = {str(row["id"]): row for row in rows}
        for message in messages:
            structured = message.get("cards")
            cards = structured.get("cards", []) if isinstance(structured, dict) else []
            for card in cards:
                if not isinstance(card, dict) or card.get("card_type") != "PlanCard":
                    continue
                refs = card.get("entity_refs")
                if not isinstance(refs, dict):
                    continue
                approval = current.get(str(refs.get("approval_id")))
                if approval is None:
                    continue
                refs["approval_version"] = int(approval["approval_version"])
                refs["approval_status"] = str(approval["status"])
                payload = card.get("payload")
                if isinstance(payload, dict):
                    payload["objective_summary"] = approval["objective_summary"]
                    payload["weekly_minutes"] = int(approval["weekly_minutes"])

    @staticmethod
    def _specialist_response(row: dict[str, Any] | None) -> dict[str, object] | None:
        if row is None:
            return None
        return {"code": row["code"], "name": row["name"], "icon": row["icon"]}

    @staticmethod
    def _conversation_response(row: dict[str, Any]) -> dict[str, object]:
        raw_metadata = row.get("specialist_metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        specialist = None
        if row.get("specialist_code"):
            specialist = {
                "code": row["specialist_code"],
                "name": row["specialist_name"],
                "icon": str(metadata.get("icon", "AI")),
            }
        active_run = None
        if row.get("active_run_id"):
            active_run = {"id": row["active_run_id"], "status": row["active_run_status"]}
        last_message = None
        if row.get("last_message_created_at"):
            last_message = {
                "content": str(row.get("last_message_content", ""))[:120],
                "created_at": row["last_message_created_at"],
            }
        return {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "specialist": specialist,
            "last_message": last_message,
            "last_message_at": row.get("last_message_at"),
            "active_run": active_run,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
