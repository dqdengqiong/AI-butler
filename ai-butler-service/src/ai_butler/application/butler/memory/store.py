"""LangGraph PostgresStore 的统一语义索引与 TTL 配置。"""

from __future__ import annotations

from collections.abc import Sequence

from langgraph.store.base import TTLConfig
from langgraph.store.postgres.base import PostgresIndexConfig

from ai_butler.adapters.embedding import EmbeddingProvider


class LangGraphEmbeddingAdapter:
    """把项目 embedding provider 适配为 LangGraph 支持的异步批量函数。"""

    def __init__(self, provider: EmbeddingProvider) -> None:
        self._provider = provider

    async def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        values = tuple(str(value) for value in texts)
        return [list(value) for value in await self._provider.embed_many(values)]


def store_index_config(provider: EmbeddingProvider) -> PostgresIndexConfig:
    """仅 statement 参与 1024 维 cosine 语义索引。"""

    if provider.dimensions != 1024:
        raise ValueError("long-term memory embedding dimensions must be 1024")
    return {
        "dims": provider.dimensions,
        "embed": LangGraphEmbeddingAdapter(provider),
        "fields": ["statement"],
        "distance_type": "cosine",
    }


STORE_TTL_CONFIG: TTLConfig = {
    "refresh_on_read": False,
    # 清理由 Scheduler 显式调用 sweep_ttl；API/Worker 不启动进程内 sweeper。
    "sweep_interval_minutes": None,
}
