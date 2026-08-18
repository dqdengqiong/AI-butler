from __future__ import annotations

import hashlib
from time import perf_counter
from typing import Literal, Protocol

from ai_butler.adapters.llm import (
    ModelInvocation,
    ModelInvocationRecorder,
    ModelTask,
    NullModelInvocationRecorder,
)


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    async def embed(self, text: str) -> list[float]: ...


class EmbeddingProviderError(RuntimeError):
    """不携带上游正文的向量模型错误。"""


class FakeEmbeddingProvider:
    """Deterministic local embedding for tests; never used as a production model."""

    model = "fake-embedding-v1"
    dimensions = 8

    async def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [round((digest[index] / 127.5) - 1.0, 6) for index in range(self.dimensions)]


class OpenAICompatibleEmbeddingProvider:
    """OpenAI-compatible Embeddings 适配器；维度由已固定模型配置决定。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dimensions: int,
        *,
        provider: str = "openai-compatible",
        recorder: ModelInvocationRecorder | None = None,
    ) -> None:
        if not api_key or not model:
            raise ValueError("embedding credentials and model are required")
        if dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, max_retries=0)
        self.model = model
        self.dimensions = dimensions
        self._provider = provider
        self._recorder = recorder or NullModelInvocationRecorder()

    async def embed(self, text: str) -> list[float]:
        started = perf_counter()
        try:
            response = await self._client.embeddings.create(
                model=self.model,
                input=text,
                dimensions=self.dimensions,
            )
        except Exception as exc:
            await self._record("FAILED", started, type(exc).__name__)
            raise EmbeddingProviderError("embedding provider request failed") from exc
        embedding = response.data[0].embedding
        if len(embedding) != self.dimensions:
            await self._record("FAILED", started, "EmbeddingDimensionMismatch")
            raise ValueError("embedding dimension mismatch")
        usage = getattr(response, "usage", None)
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        await self._record(
            "SUCCEEDED",
            started,
            None,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            cached_input_tokens=cached,
        )
        return embedding

    async def _record(
        self,
        status: Literal["SUCCEEDED", "FAILED"],
        started: float,
        error_class: str | None,
        *,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
    ) -> None:
        await self._recorder.record(
            ModelInvocation(
                request_id=None,
                run_id=None,
                task=ModelTask.EMBEDDING,
                provider=self._provider,
                model=self.model,
                prompt_version="embedding-v1",
                schema_version=None,
                attempt=1,
                route_role="PRIMARY",
                status=status,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=0,
                duration_ms=round((perf_counter() - started) * 1000),
                error_class=error_class,
            )
        )
