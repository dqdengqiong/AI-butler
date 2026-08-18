"""初始化验证环境 Qdrant collection 与强制过滤索引。"""

from __future__ import annotations

import asyncio

from ai_butler.adapters.model_routing import load_model_routing
from ai_butler.adapters.vector import QdrantVectorStore
from ai_butler.config import get_settings


async def setup_knowledge() -> None:
    settings = get_settings()
    dimensions = (
        load_model_routing(settings.model_routing_file, settings.app_env).embedding.dimensions
        if settings.model_routing_enabled
        else 8
    )
    vector_store = QdrantVectorStore(
        settings.qdrant_url,
        settings.qdrant_collection,
        dimensions,
    )
    await vector_store.setup()


def main() -> None:
    asyncio.run(setup_knowledge())
    print("initialized Qdrant collection and tenant payload indexes")


if __name__ == "__main__":
    main()
