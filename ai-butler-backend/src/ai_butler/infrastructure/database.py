from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine


class Database(Protocol):
    async def ping(self) -> bool: ...

    async def close(self) -> None: ...

    def transaction(self) -> AsyncIterator[AsyncConnection]: ...


class AsyncDatabase:
    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)

    async def ping(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    async def close(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        """提供由 application service 控制的短事务边界。"""

        async with self._engine.begin() as connection:
            yield connection

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        """提供只读或显式提交的连接，不在网络等待期间持有事务。"""

        async with self._engine.connect() as connection:
            yield connection
