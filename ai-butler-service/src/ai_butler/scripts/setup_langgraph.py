from __future__ import annotations

import asyncio

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore

from ai_butler.application.butler.memory.store import STORE_TTL_CONFIG, store_index_config
from ai_butler.application.butler.support import build_embedding_provider
from ai_butler.config import get_settings


async def setup_internal_tables(database_url: str) -> None:
    settings = get_settings()
    embedding_provider = build_embedding_provider(settings)
    async with AsyncPostgresSaver.from_conn_string(database_url) as checkpointer:
        await checkpointer.setup()
    async with AsyncPostgresStore.from_conn_string(
        database_url,
        index=store_index_config(embedding_provider),
        ttl=STORE_TTL_CONFIG,
    ) as store:
        await store.setup()


def grant_runtime_access(database_url: str) -> None:
    statements = (
        "GRANT USAGE ON SCHEMA public TO butler_app,butler_test",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        "TO butler_app,butler_test",
        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO butler_app,butler_test",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO butler_app,butler_test",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO butler_app,butler_test",
    )
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
        connection.commit()


def main() -> None:
    database_url = get_settings().langgraph_migration_database_url
    asyncio.run(setup_internal_tables(database_url))
    grant_runtime_access(database_url)
    print("initialized LangGraph PostgreSQL checkpointer/store tables")


if __name__ == "__main__":
    main()
