from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from ai_butler.application.butler.private_retrieval import PrivateEvidenceRetriever

USER_ID = UUID("00000000-0000-0000-0000-000000000201")


class EmptyResult:
    def all(self) -> list[tuple[object, ...]]:
        return []


class EmptyKnowledgeConnection:
    async def execute(self, *_args: object, **_kwargs: object) -> EmptyResult:
        return EmptyResult()


class EmptyKnowledgeDatabase:
    @asynccontextmanager
    async def connect(self):  # type: ignore[no-untyped-def]
        yield EmptyKnowledgeConnection()


class UnexpectedEmbeddingProvider:
    async def embed(self, _query: str) -> list[float]:
        raise AssertionError("empty private knowledge must not call the embedding provider")


class UnexpectedVectorStore:
    async def search(self, *_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise AssertionError("empty private knowledge must not call the vector store")


@pytest.mark.asyncio
async def test_private_retrieval_skips_vector_dependencies_without_ready_documents() -> None:
    """模型误判需要私有资料时，空资料库不得阻断后续计划或联网流程。"""

    context = SimpleNamespace(
        database=EmptyKnowledgeDatabase(),
        embedding_provider=UnexpectedEmbeddingProvider(),
        vector_store=UnexpectedVectorStore(),
        settings=SimpleNamespace(search_candidate_results=5),
    )
    retriever = PrivateEvidenceRetriever(context)  # type: ignore[arg-type]

    assert await retriever._retrieve_private_evidence(USER_ID, "合成查询", ()) == ()
