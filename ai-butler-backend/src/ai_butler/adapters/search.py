"""供应商中立的联网搜索适配器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import httpx


class SearchError(RuntimeError):
    """搜索供应商错误；消息不得包含查询正文或上游响应体。"""


class SearchUnavailableError(SearchError):
    """临时网络或供应商故障，可由 run 重试。"""


class SearchRateLimitError(SearchUnavailableError):
    """供应商限流，可由 run 重试。"""


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """最小化后的公共检索请求，不携带身份、附件或任意过滤表达式。"""

    query: str
    max_results: int = 5
    include_domains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchResult:
    """供应商中立证据；网页内容始终作为不可信数据处理。"""

    evidence_ref: str
    title: str
    content: str
    score: float
    url: str | None
    source_organization: str | None = None
    published_at: datetime | None = None
    source_type: str = "WEB"
    knowledge_chunk_id: UUID | None = None


class SearchProvider(Protocol):
    """联网检索边界。调用方负责查询预算、引用 Gate 和持久化。"""

    async def search(self, request: SearchRequest) -> tuple[SearchResult, ...]: ...


class FakeSearchProvider:
    """离线验收搜索器，返回明确标记的合成来源且不访问网络。"""

    async def search(self, request: SearchRequest) -> tuple[SearchResult, ...]:
        if not request.query.strip():
            return ()
        return (
            SearchResult(
                evidence_ref="fake-civil-service-source",
                title="合成公考检索来源（非真实公告）",
                source_organization="AI Butler Test Fixtures",
                content="合成检索资料：公务员备考通常包含行测、申论训练与周期复盘。",
                score=1.0,
                url=None,
                source_type="KNOWLEDGE",
            ),
        )[: request.max_results]


class TavilySearchProvider:
    """调用 Tavily Search API，并仅暴露回答所需的最小来源字段。

    固定关闭供应商生成答案与原始网页正文，避免第二个模型答案绕过本地
    Evidence Gate；HTTP 错误正文可能包含查询信息，因此不会向上层传播。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Tavily API key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def search(self, request: SearchRequest) -> tuple[SearchResult, ...]:
        payload: dict[str, object] = {
            "query": request.query,
            "search_depth": "basic",
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "max_results": request.max_results,
        }
        if request.include_domains:
            payload["include_domains"] = list(request.include_domains)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/search",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SearchUnavailableError("search provider unavailable") from exc
        if response.status_code == 429:
            raise SearchRateLimitError("search provider rate limited")
        if response.status_code >= 500:
            raise SearchUnavailableError("search provider unavailable")
        try:
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError("search provider returned an invalid response") from exc

        results: list[SearchResult] = []
        for index, item in enumerate(body.get("results", [])):
            if not isinstance(item, dict):
                continue
            title, content, url = item.get("title"), item.get("content"), item.get("url")
            if (
                not isinstance(title, str)
                or not title.strip()
                or not isinstance(content, str)
                or not content.strip()
                or not isinstance(url, str)
                or not url.strip()
            ):
                continue
            score = item.get("score")
            published_at: datetime | None = None
            published = item.get("published_date")
            if isinstance(published, str):
                try:
                    published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if published_at.tzinfo is None:
                        published_at = published_at.replace(tzinfo=UTC)
                except ValueError:
                    published_at = None
            results.append(
                SearchResult(
                    evidence_ref=f"tavily-{index + 1}",
                    title=title.strip(),
                    content=content.strip(),
                    score=float(score) if isinstance(score, int | float) else 0.0,
                    url=url.strip(),
                    published_at=published_at,
                )
            )
        return tuple(results)


_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_LONG_NUMBER = re.compile(r"(?<!\d)\d{7,}(?!\d)")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def minimize_public_query(value: str, *, max_length: int = 300) -> str:
    """移除常见直接标识符并限制长度，避免把无关隐私发送给搜索供应商。"""

    normalized = " ".join(value.split())
    normalized = _URL.sub(" ", normalized)
    normalized = _EMAIL.sub("[已省略邮箱]", normalized)
    normalized = _LONG_NUMBER.sub("[已省略编号]", normalized)
    return " ".join(normalized.split())[:max_length].strip()
