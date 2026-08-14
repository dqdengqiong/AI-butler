from __future__ import annotations

import hashlib
from typing import Protocol


class EmbeddingProvider(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class FakeEmbeddingProvider:
    """Deterministic local embedding for tests; never used as a production model."""

    dimensions = 8

    async def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [round((digest[index] / 127.5) - 1.0, 6) for index in range(self.dimensions)]


class OpenAICompatibleEmbeddingProvider:
    """OpenAI-compatible Embeddings 适配器；维度由已固定模型配置决定。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        if not api_key or not model:
            raise ValueError("embedding credentials and model are required")
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        self._model = model

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(model=self._model, input=text)
        return response.data[0].embedding
