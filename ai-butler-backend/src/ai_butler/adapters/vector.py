"""Qdrant 向量存储适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx


class VectorStoreError(RuntimeError):
    """向量存储故障；不传播可能包含私有正文的上游响应。"""


@dataclass(frozen=True, slots=True)
class VectorPoint:
    point_id: UUID
    vector: tuple[float, ...]
    tenant_id: UUID
    document_id: UUID
    chunk_id: UUID


class VectorStore(Protocol):
    """私有资料向量边界，tenant_id 必须来自服务端用户上下文。"""

    async def upsert(self, points: tuple[VectorPoint, ...]) -> None: ...

    async def search(
        self, tenant_id: UUID, vector: list[float], limit: int
    ) -> tuple[UUID, ...]: ...

    async def delete_document(self, tenant_id: UUID, document_id: UUID) -> None: ...


class QdrantVectorStore:
    """使用 Qdrant REST API 保存和检索租户隔离的私有资料向量。"""

    def __init__(
        self,
        base_url: str,
        collection: str,
        dimensions: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._collection = collection
        self._dimensions = dimensions
        self._transport = transport

    async def _request(
        self, method: str, path: str, payload: dict[str, object]
    ) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.request(method, f"{self._base_url}{path}", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VectorStoreError("vector store request failed") from exc
        return body if isinstance(body, dict) else {}

    async def _ensure_collection(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.get(f"{self._base_url}/collections/{self._collection}")
        except httpx.HTTPError as exc:
            raise VectorStoreError("vector store request failed") from exc
        if response.status_code == 200:
            return
        if response.status_code != 404:
            raise VectorStoreError("vector store request failed")
        await self._request(
            "PUT",
            f"/collections/{self._collection}",
            {"vectors": {"size": self._dimensions, "distance": "Cosine"}},
        )

    async def upsert(self, points: tuple[VectorPoint, ...]) -> None:
        if not points:
            return
        await self._ensure_collection()
        await self._request(
            "PUT",
            f"/collections/{self._collection}/points?wait=true",
            {
                "points": [
                    {
                        "id": str(point.point_id),
                        "vector": list(point.vector),
                        "payload": {
                            "tenant_id": str(point.tenant_id),
                            "document_id": str(point.document_id),
                            "chunk_id": str(point.chunk_id),
                        },
                    }
                    for point in points
                ]
            },
        )

    async def search(self, tenant_id: UUID, vector: list[float], limit: int) -> tuple[UUID, ...]:
        await self._ensure_collection()
        body = await self._request(
            "POST",
            f"/collections/{self._collection}/points/query",
            {
                "query": vector,
                "filter": {"must": [{"key": "tenant_id", "match": {"value": str(tenant_id)}}]},
                "limit": limit,
                "with_payload": True,
            },
        )
        result = body.get("result")
        points = result.get("points", []) if isinstance(result, dict) else []
        chunk_ids: list[UUID] = []
        for point in points:
            if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
                continue
            chunk_id = point["payload"].get("chunk_id")
            try:
                chunk_ids.append(UUID(str(chunk_id)))
            except (TypeError, ValueError):
                continue
        return tuple(chunk_ids)

    async def delete_document(self, tenant_id: UUID, document_id: UUID) -> None:
        await self._ensure_collection()
        await self._request(
            "POST",
            f"/collections/{self._collection}/points/delete?wait=true",
            {
                "filter": {
                    "must": [
                        {"key": "tenant_id", "match": {"value": str(tenant_id)}},
                        {"key": "document_id", "match": {"value": str(document_id)}},
                    ]
                }
            },
        )
