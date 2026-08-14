"""RAG 证据安全 Gate 与结构化引用渲染。"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from ai_butler.adapters.search import SearchResult
from ai_butler.domain.errors import ButlerError


class AnswerSegmentV1(BaseModel):
    """模型可生成的最小回答片段；引用只能指向服务端提供的 evidence ref。"""

    text: str = Field(min_length=1, max_length=4000)
    evidence_refs: tuple[str, ...] = ()


class RagAnswerV1(BaseModel):
    """版本化 RAG 输出；服务端负责编号和 Markdown 拼接。"""

    schema_version: str = "1.0"
    segments: tuple[AnswerSegmentV1, ...] = Field(min_length=1, max_length=20)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NumberedEvidence:
    index: int
    result: SearchResult
    canonical_url: str | None
    domain: str | None
    source_level: str


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
        self, results: tuple[SearchResult, ...], *, limit: int
    ) -> tuple[NumberedEvidence, ...]:
        """按 canonical URL/evidence ref 去重，保留供应商排序且施加结果预算。"""

        selected: list[NumberedEvidence] = []
        seen: set[str] = set()
        for result in results:
            canonical, domain = self.canonicalize_url(result.url)
            key = canonical or result.evidence_ref
            if key in seen:
                continue
            seen.add(key)
            selected.append(
                NumberedEvidence(
                    len(selected) + 1,
                    result,
                    canonical,
                    domain,
                    self._source_level(result, domain),
                )
            )
            if len(selected) >= limit:
                break
        return tuple(selected)

    @staticmethod
    def render(answer: RagAnswerV1, evidence: tuple[NumberedEvidence, ...]) -> str:
        """将合法 evidence ref 渲染为稳定 `[n]`，未知引用直接失败而非静默丢弃。"""

        indexes = {item.result.evidence_ref: item.index for item in evidence}
        rendered: list[str] = []
        for segment in answer.segments:
            unknown = [ref for ref in segment.evidence_refs if ref not in indexes]
            if unknown:
                raise ButlerError("CITATION_REFERENCE_INVALID", "回答包含无效引用", 422)
            suffix = "".join(f"[{indexes[ref]}]" for ref in dict.fromkeys(segment.evidence_refs))
            rendered.append(f"{segment.text}{suffix}")
        return "\n\n".join(rendered)
