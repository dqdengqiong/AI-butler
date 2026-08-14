from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from ai_butler.adapters.documents import chunk_text, extract_text
from ai_butler.adapters.search import (
    FakeSearchProvider,
    SearchRateLimitError,
    SearchRequest,
    SearchResult,
    TavilySearchProvider,
    minimize_public_query,
)
from ai_butler.adapters.vector import QdrantVectorStore, VectorPoint
from ai_butler.agent.evidence import AnswerSegmentV1, EvidenceGate, RagAnswerV1
from ai_butler.domain.errors import ButlerError

USER_ID = UUID("00000000-0000-0000-0000-000000000101")
DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000102")
CHUNK_ID = UUID("00000000-0000-0000-0000-000000000103")


@pytest.mark.asyncio
async def test_fake_search_and_query_minimization_are_deterministic() -> None:
    query = minimize_public_query(
        "查询 https://private.invalid 用户 user@example.com 编号 123456789 的公考政策"
    )
    assert "private.invalid" not in query
    assert "user@example.com" not in query
    assert "123456789" not in query
    first = await FakeSearchProvider().search(SearchRequest(query=query))
    second = await FakeSearchProvider().search(SearchRequest(query=query))
    assert first == second
    assert first[0].source_type == "KNOWLEDGE"


@pytest.mark.asyncio
async def test_tavily_adapter_uses_bounded_safe_request_and_maps_results() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        assert body["search_depth"] == "basic"
        assert body["include_answer"] is False
        assert body["include_raw_content"] is False
        assert body["max_results"] == 2
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "官方公告",
                        "url": "https://example.gov.cn/notice",
                        "content": "公告证据片段",
                        "score": 0.9,
                        "published_date": "2026-08-01T00:00:00Z",
                    }
                ]
            },
        )

    provider = TavilySearchProvider(
        "synthetic-key", "https://api.tavily.com", 1, httpx.MockTransport(handler)
    )
    results = await provider.search(SearchRequest("公考公告", max_results=2))
    assert results[0].title == "官方公告"
    assert results[0].score == 0.9
    assert results[0].published_at is not None


@pytest.mark.asyncio
async def test_tavily_rate_limit_is_retryable_provider_error() -> None:
    provider = TavilySearchProvider(
        "synthetic-key",
        "https://api.tavily.com",
        1,
        httpx.MockTransport(lambda _: httpx.Response(429)),
    )
    with pytest.raises(SearchRateLimitError):
        await provider.search(SearchRequest("公考公告"))


def test_evidence_gate_deduplicates_numbers_and_rejects_unknown_refs() -> None:
    gate = EvidenceGate(("gov.cn",))
    evidence = gate.normalize(
        (
            SearchResult("a", "A", "片段 A", 1, "https://EXAMPLE.gov.cn/a#top"),
            SearchResult("b", "A duplicate", "片段 B", 0.9, "https://example.gov.cn/a"),
        ),
        limit=5,
    )
    assert len(evidence) == 1
    assert evidence[0].source_level == "OFFICIAL"
    answer = RagAnswerV1(segments=(AnswerSegmentV1(text="结论", evidence_refs=("a",)),))
    assert gate.render(answer, evidence) == "结论[1]"
    with pytest.raises(ButlerError) as error:
        gate.render(
            RagAnswerV1(segments=(AnswerSegmentV1(text="伪造结论", evidence_refs=("missing",)),)),
            evidence,
        )
    assert error.value.code == "CITATION_REFERENCE_INVALID"
    with pytest.raises(ButlerError):
        gate.normalize(
            (SearchResult("unsafe", "Unsafe", "x", 1, "http://127.0.0.1/private"),),
            limit=5,
        )


def test_text_extraction_and_chunking_are_bounded() -> None:
    assert extract_text("学习资料".encode(), "text/plain") == "学习资料"
    chunks = chunk_text("a" * 3200)
    assert len(chunks) == 3
    assert all(len(chunk.content) <= 1500 for chunk in chunks)
    with pytest.raises(ValueError):
        extract_text(b"data", "application/octet-stream")


@pytest.mark.asyncio
async def test_qdrant_adapter_filters_tenant_and_maps_chunk_ids() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append(body)
        if request.url.path.endswith("/points/query"):
            assert body["filter"] == {
                "must": [{"key": "tenant_id", "match": {"value": str(USER_ID)}}]
            }
            return httpx.Response(
                200, json={"result": {"points": [{"payload": {"chunk_id": str(CHUNK_ID)}}]}}
            )
        return httpx.Response(200, json={"result": True})

    store = QdrantVectorStore("http://qdrant", "test", 2, httpx.MockTransport(handler))
    await store.upsert(
        (
            VectorPoint(
                point_id=CHUNK_ID,
                vector=(0.1, 0.2),
                tenant_id=USER_ID,
                document_id=DOCUMENT_ID,
                chunk_id=CHUNK_ID,
            ),
        )
    )
    assert await store.search(USER_ID, [0.1, 0.2], 5) == (CHUNK_ID,)
    await store.delete_document(USER_ID, DOCUMENT_ID)
    assert len(requests) == 6
