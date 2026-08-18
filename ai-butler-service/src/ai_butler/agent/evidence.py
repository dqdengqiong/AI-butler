"""RAG 证据安全 Gate 与结构化引用渲染。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from ai_butler.adapters.search import SearchResult
from ai_butler.domain.errors import ButlerError


class AnswerSegmentV1(BaseModel):
    """模型可生成的最小回答片段；引用只能指向服务端提供的 evidence ref。"""

    text: str = Field(min_length=1, max_length=4000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=5)


class RagAnswerV1(BaseModel):
    """版本化 RAG 输出；服务端负责编号和 Markdown 拼接。"""

    schema_version: Literal["1.0"] = "1.0"
    segments: tuple[AnswerSegmentV1, ...] = Field(min_length=1, max_length=20)
    warnings: tuple[str, ...] = Field(default=(), max_length=5)


@dataclass(frozen=True, slots=True)
class NumberedEvidence:
    index: int
    result: SearchResult
    canonical_url: str | None
    domain: str | None
    source_level: str
    estimated_tokens: int


_NUMBER = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|(?<![\w.])\d+(?:[.,]\d+)*(?:%|年|月|日|时|分)?"
)


def estimate_tokens(value: str) -> int:
    """跨供应商的保守预算估算；真实调用仍由模型网关执行最终上限检查。"""

    return max(1, (len(value.encode("utf-8")) + 2) // 3)


def _truncate_to_tokens(value: str, max_tokens: int) -> str:
    if estimate_tokens(value) <= max_tokens:
        return value
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(value[:middle] + "…") <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return value[:low].rstrip() + "…"


class EvidenceGate:
    """校验外部来源、去重并拒绝模型引用不存在的 evidence ref。"""

    def __init__(self, official_domains: tuple[str, ...] = ()) -> None:
        self._official_domains = tuple(
            domain.lower().strip().lstrip(".") for domain in official_domains if domain.strip()
        )

    def _source_level(self, result: SearchResult, domain: str | None) -> str:
        if result.source_type == "PRIVATE_FILE":
            return "PRIVATE"
        if domain and any(
            domain == official or domain.endswith(f".{official}")
            for official in self._official_domains
        ):
            return "OFFICIAL"
        return "GENERAL"

    @staticmethod
    def canonicalize_url(value: str | None) -> tuple[str | None, str | None]:
        if value is None:
            return None, None
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            raise ButlerError("SOURCE_URL_UNSAFE", "引用来源地址不安全", 422)
        if host == "localhost" or host.endswith(".local"):
            raise ButlerError("SOURCE_URL_UNSAFE", "引用来源地址不安全", 422)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ButlerError("SOURCE_URL_UNSAFE", "引用来源地址不安全", 422)
        port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
        canonical = urlunsplit(("https", f"{host}{port}", parsed.path or "/", parsed.query, ""))
        return canonical, host

    def normalize(
        self,
        results: tuple[SearchResult, ...],
        *,
        limit: int,
        token_budget: int | None = None,
        max_item_tokens: int = 700,
    ) -> tuple[NumberedEvidence, ...]:
        """安全去重、轻量重排，并在总 Token 和单文档预算内选择证据。"""

        candidates: list[tuple[SearchResult, str | None, str | None, str]] = []
        seen: set[str] = set()
        for result in results:
            canonical, domain = self.canonicalize_url(result.url)
            normalized_content = " ".join(result.content.split())
            key = canonical or normalized_content
            if key in seen:
                continue
            seen.add(key)
            candidates.append((result, canonical, domain, self._source_level(result, domain)))

        source_boost = {"OFFICIAL": 0.10, "PRIVATE": 0.02, "GENERAL": 0.0}
        candidates.sort(
            key=lambda item: item[0].score + source_boost[item[3]],
            reverse=True,
        )
        remaining = token_budget if token_budget is not None else 2**31 - 1
        selected: list[NumberedEvidence] = []
        per_document: dict[object, int] = {}
        for result, canonical, domain, source_level in candidates:
            document_key: object | None = result.document_id or canonical
            if document_key is not None and per_document.get(document_key, 0) >= 2:
                continue
            content = _truncate_to_tokens(result.content, max_item_tokens)
            selected_result = replace(result, content=content)
            serialized = self.serialize(selected_result, source_level, domain)
            cost = estimate_tokens(serialized)
            if cost > remaining:
                continue
            selected.append(
                NumberedEvidence(
                    len(selected) + 1,
                    selected_result,
                    canonical,
                    domain,
                    source_level,
                    cost,
                )
            )
            remaining -= cost
            if document_key is not None:
                per_document[document_key] = per_document.get(document_key, 0) + 1
            if len(selected) >= limit:
                break
        return tuple(selected)

    @staticmethod
    def serialize(result: SearchResult, source_level: str, domain: str | None) -> str:
        published = result.published_at.isoformat() if result.published_at else "unknown"
        return (
            f"REF: {result.evidence_ref}\nTITLE: {result.title}\n"
            f"SOURCE_LEVEL: {source_level}\nDOMAIN: {domain or 'private'}\n"
            f"PUBLISHED_AT: {published}\nCONTENT:\n{result.content}"
        )

    @staticmethod
    def validate_answer(answer: RagAnswerV1, evidence: tuple[NumberedEvidence, ...]) -> None:
        """校验引用集合，并阻断证据中不存在的确定性数字。"""

        by_ref = {item.result.evidence_ref: item for item in evidence}
        for segment in answer.segments:
            unknown = [ref for ref in segment.evidence_refs if ref not in by_ref]
            if unknown:
                raise ButlerError("CITATION_REFERENCE_INVALID", "回答包含无效引用", 422)
            cited = "\n".join(by_ref[ref].result.content for ref in segment.evidence_refs)
            unsupported = [
                number for number in _NUMBER.findall(segment.text) if number not in cited
            ]
            if unsupported:
                raise ButlerError("CITATION_SUPPORT_INVALID", "回答包含来源不支持的数字", 422)

    @staticmethod
    def render(answer: RagAnswerV1, evidence: tuple[NumberedEvidence, ...]) -> str:
        """将合法 evidence ref 渲染为稳定 `[n]`，未知引用直接失败而非静默丢弃。"""

        EvidenceGate.validate_answer(answer, evidence)
        indexes = {item.result.evidence_ref: item.index for item in evidence}
        rendered: list[str] = []
        for segment in answer.segments:
            suffix = "".join(f"[{indexes[ref]}]" for ref in dict.fromkeys(segment.evidence_refs))
            rendered.append(f"{segment.text}{suffix}")
        rendered.extend(f"> 提示：{warning}" for warning in answer.warnings)
        return "\n\n".join(rendered)
