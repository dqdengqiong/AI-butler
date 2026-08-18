"""公共只读上下文、检索与记忆工具实现。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text

from ai_butler.adapters.search import SearchProvider, SearchRequest, SearchResult
from ai_butler.infrastructure.database import AsyncDatabase


async def read_plan_context(
    database: AsyncDatabase, user_id: UUID
) -> tuple[dict[str, object], ...]:
    async with database.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT id,title,status,current_revision_id FROM plans "
                    "WHERE user_id=:user_id AND status='ACTIVE' "
                    "ORDER BY updated_at DESC LIMIT 10"
                ),
                {"user_id": user_id},
            )
        ).mappings()
        return tuple(dict(row) for row in rows)


async def read_task_context(
    database: AsyncDatabase, user_id: UUID, start: date, end: date
) -> tuple[dict[str, object], ...]:
    async with database.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT t.id,t.title,t.scheduled_date,t.status,t.expected_minutes,t.priority "
                    "FROM tasks t JOIN plans p ON p.id=t.plan_id "
                    "WHERE t.user_id=:user_id AND p.status='ACTIVE' "
                    "AND t.scheduled_date BETWEEN :start AND :end "
                    "ORDER BY t.scheduled_date,t.priority,t.id LIMIT 100"
                ),
                {"user_id": user_id, "start": start, "end": end},
            )
        ).mappings()
        return tuple(dict(row) for row in rows)


async def search_public_knowledge(
    provider: SearchProvider, query: str, max_results: int
) -> tuple[SearchResult, ...]:
    return await provider.search(SearchRequest(query=query, max_results=max_results))


async def search_private_knowledge(
    retriever: Callable[[UUID, str, tuple[UUID, ...]], Awaitable[tuple[SearchResult, ...]]],
    user_id: UUID,
    query: str,
    allowed_file_ids: tuple[UUID, ...],
) -> tuple[SearchResult, ...]:
    return await retriever(user_id, query, allowed_file_ids)


async def handle_memory_command[T](
    handler: Callable[[UUID, str], Awaitable[T]], user_id: UUID, command: str
) -> T:
    return await handler(user_id, command)


async def prepare_plan_preview[T](
    builder: Callable[..., Awaitable[T]], **verified_inputs: Any
) -> T:
    """调用受控 Planner；调用者只能传入代码已验证和绑定的输入。"""

    return await builder(**verified_inputs)
