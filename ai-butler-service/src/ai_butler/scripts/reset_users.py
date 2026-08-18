"""一次性清理旧身份模型产生的全部用户数据。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from ai_butler.adapters.model_routing import load_model_routing
from ai_butler.adapters.vector import QdrantVectorStore
from ai_butler.config import get_settings
from ai_butler.infrastructure.database import AsyncDatabase


async def reset_users(confirm: bool) -> int:
    """先清理外部私有数据，再级联删除用户事实。

    只有显式确认才执行。任一外部删除失败时不会删除 PostgreSQL 用户行，方便
    运维修复后重试；公开知识文档不在清理范围。
    """

    if not confirm:
        raise SystemExit("refusing to delete users without --confirm-delete-all-users")
    settings = get_settings()
    if settings.object_storage_backend != "local":
        raise SystemExit("user reset currently supports local object storage only")
    database = AsyncDatabase(settings.migration_database_url)
    dimensions = (
        load_model_routing(settings.model_routing_file, settings.app_env).embedding.dimensions
        if settings.model_routing_enabled
        else 8
    )
    vector_store = QdrantVectorStore(settings.qdrant_url, settings.qdrant_collection, dimensions)
    try:
        async with database.connect() as connection:
            documents = (
                (
                    await connection.execute(
                        text(
                            "SELECT id,owner_user_id FROM knowledge_documents "
                            "WHERE owner_user_id IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
            object_keys = (
                (
                    await connection.execute(
                        text("SELECT object_key FROM stored_files WHERE user_id IS NOT NULL")
                    )
                )
                .scalars()
                .all()
            )
        for document in documents:
            await vector_store.delete_document(
                UUID(str(document["owner_user_id"])), UUID(str(document["id"]))
            )
        root = settings.object_storage_local_path.resolve()
        for object_key in object_keys:
            target = (root / str(object_key)).resolve()
            if not target.is_relative_to(root):
                raise RuntimeError("stored object key escapes configured storage root")
            if target.is_file():
                target.unlink()
                _remove_empty_parents(target.parent, root)
        async with database.transaction() as connection:
            result = await connection.execute(text("DELETE FROM users"))
            return int(result.rowcount or 0)
    finally:
        await database.close()


def _remove_empty_parents(current: Path, root: Path) -> None:
    """仅清理由本次文件删除留下的空目录，绝不越过配置根目录。"""

    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="delete all legacy AI Butler users")
    parser.add_argument("--confirm-delete-all-users", action="store_true")
    arguments = parser.parse_args()
    deleted = asyncio.run(reset_users(arguments.confirm_delete_all_users))
    print(f"deleted {deleted} users and their account-owned data")


if __name__ == "__main__":
    main()
