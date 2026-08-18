from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from ai_butler.adapters.documents import chunk_text, extract_text
from ai_butler.adapters.llm import FakeLLM
from ai_butler.adapters.search import (
    FakeSearchProvider,
    SearchRateLimitError,
    SearchRequest,
    SearchResult,
    TavilySearchProvider,
    minimize_public_query,
    normalize_private_query,
)
from ai_butler.adapters.vector import (
    QdrantVectorStore,
    VectorPoint,
    VectorSearchHit,
    VectorStoreError,
)
from ai_butler.agent.evidence import (
    AnswerSegmentV1,
    EvidenceGate,
    RagAnswerV1,
)
from ai_butler.application.butler.evidence_execution import EvidenceExecutionService
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
    assert "123456789" in normalize_private_query("  编号 123456789   的资料 ")
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


def test_evidence_selection_respects_rank_token_and_document_budgets() -> None:
    gate = EvidenceGate(("gov.cn",))
    second_document = UUID("00000000-0000-0000-0000-000000000104")
    results = (
        SearchResult("general", "普通", "普通内容" * 80, 0.9, "https://example.com/a"),
        SearchResult("official", "官方", "官方内容" * 80, 0.9, "https://exam.gov.cn/a"),
        SearchResult(
            "private-1",
            "资料一",
            "资料内容" * 80,
            0.8,
            None,
            source_type="PRIVATE_FILE",
            document_id=DOCUMENT_ID,
        ),
        SearchResult(
            "private-2",
            "资料二",
            "资料内容二" * 80,
            0.79,
            None,
            source_type="PRIVATE_FILE",
            document_id=DOCUMENT_ID,
        ),
        SearchResult(
            "private-3",
            "资料三",
            "资料内容三" * 80,
            0.78,
            None,
            source_type="PRIVATE_FILE",
            document_id=DOCUMENT_ID,
        ),
        SearchResult(
            "private-other",
            "其他资料",
            "其他内容" * 80,
            0.77,
            None,
            source_type="PRIVATE_FILE",
            document_id=second_document,
        ),
    )
    selected = gate.normalize(results, limit=5, token_budget=650, max_item_tokens=180)

    assert selected[0].result.evidence_ref == "official"
    assert sum(item.estimated_tokens for item in selected) <= 650
    assert sum(item.result.document_id == DOCUMENT_ID for item in selected) <= 2
    assert len(selected) < len(results)


def test_evidence_gate_rejects_unsupported_numbers() -> None:
    gate = EvidenceGate()
    evidence = gate.normalize(
        (SearchResult("source", "公告", "截止日期为 2026 年 10 月 8 日", 1, None),),
        limit=5,
    )
    with pytest.raises(ButlerError) as error:
        gate.render(
            RagAnswerV1(
                segments=(
                    AnswerSegmentV1(
                        text="截止日期为 2026 年 10 月 10 日",
                        evidence_refs=("source",),
                    ),
                )
            ),
            evidence,
        )
    assert error.value.code == "CITATION_SUPPORT_INVALID"


@pytest.mark.asyncio
async def test_research_answer_generation_uses_selected_evidence_refs() -> None:
    gate = EvidenceGate()
    evidence = gate.normalize(
        (
            SearchResult(
                "source",
                "公告",
                "报名截止日期为 2026 年 10 月 8 日。",
                1,
                None,
            ),
        ),
        limit=5,
        token_budget=4000,
    )
    service = object.__new__(EvidenceExecutionService)
    service.evidence_gate = gate
    service.llm = FakeLLM()

    answer = await service._generate_rag_answer("报名什么时候截止？", evidence, run_id=USER_ID)

    assert answer.segments[0].evidence_refs == ("source",)
    assert gate.render(answer, evidence).endswith("[1]")


def test_text_extraction_and_chunking_are_bounded() -> None:
    assert extract_text("学习资料".encode(), "text/plain") == "学习资料"
    chunks = chunk_text("a" * 3200)
    assert len(chunks) == 3
    assert all(len(chunk.content) <= 1500 for chunk in chunks)
    markdown_chunks = chunk_text("# 第一章\n" + "第一章内容。" * 300)
    assert markdown_chunks[0].heading_path == "第一章"
    with pytest.raises(ValueError):
        extract_text(b"data", "application/octet-stream")


@pytest.mark.asyncio
async def test_qdrant_adapter_filters_tenant_and_maps_chunk_ids() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append(body)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "points_count": 1,
                        "config": {"params": {"vectors": {"size": 2}}},
                    }
                },
            )
        if request.url.path.endswith("/points/query"):
            assert body["filter"] == {
                "must": [
                    {"key": "tenant_id", "match": {"value": str(USER_ID)}},
                    {
                        "key": "document_id",
                        "match": {"any": [str(DOCUMENT_ID)]},
                    },
                ]
            }
            return httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {
                                "score": 0.87,
                                "payload": {
                                    "chunk_id": str(CHUNK_ID),
                                    "document_id": str(DOCUMENT_ID),
                                },
                            }
                        ]
                    }
                },
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
    assert await store.search(USER_ID, [0.1, 0.2], 5, (DOCUMENT_ID,)) == (
        VectorSearchHit(CHUNK_ID, DOCUMENT_ID, 0.87),
    )
    await store.delete_document(USER_ID, DOCUMENT_ID)
    assert len(requests) == 6


@pytest.mark.asyncio
async def test_qdrant_setup_repairs_only_empty_dimension_mismatch() -> None:
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "points_count": 0,
                        "config": {"params": {"vectors": {"size": 8}}},
                    }
                },
            )
        return httpx.Response(200, json={"result": True})

    store = QdrantVectorStore("http://qdrant", "test", 1024, httpx.MockTransport(handler))
    await store.setup()

    assert requests == [
        ("GET", "/collections/test"),
        ("DELETE", "/collections/test"),
        ("PUT", "/collections/test"),
        ("PUT", "/collections/test/index"),
        ("PUT", "/collections/test/index"),
    ]


@pytest.mark.asyncio
async def test_qdrant_setup_rejects_nonempty_dimension_mismatch() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "result": {
                    "points_count": 1,
                    "config": {"params": {"vectors": {"size": 8}}},
                }
            },
        )

    store = QdrantVectorStore("http://qdrant", "test", 1024, httpx.MockTransport(handler))

    with pytest.raises(VectorStoreError, match="dimension mismatch"):
        await store.setup()
