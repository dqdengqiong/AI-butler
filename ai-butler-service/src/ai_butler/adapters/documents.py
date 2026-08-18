"""私有学习资料的受控文本抽取与分块。"""

from __future__ import annotations

import re
from dataclasses import dataclass

SUPPORTED_RAG_MIME_TYPES = frozenset({"text/plain", "text/markdown", "application/pdf"})


@dataclass(frozen=True, slots=True)
class TextChunk:
    index: int
    content: str
    heading_path: str | None = None


def extract_text(content: bytes, mime_type: str) -> str:
    """抽取允许 MIME 的文本；PDF 解析失败时显式失败，不尝试执行嵌入对象。"""

    if mime_type in {"text/plain", "text/markdown"}:
        return content.decode("utf-8")
    if mime_type == "application/pdf":
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=True)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError("unsupported knowledge MIME type")


def chunk_text(value: str, *, size: int = 1500, overlap: int = 150) -> tuple[TextChunk, ...]:
    """优先在段落或句末边界分块，并保留最近的 Markdown 标题路径。"""

    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("invalid chunk size or overlap")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    normalized = "\n".join(lines)
    if not normalized:
        return ()
    headings: list[tuple[int, str]] = []
    offset = 0
    heading_stack: list[str] = []
    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match:
            level = len(match.group(1))
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(match.group(2).strip())
            headings.append((offset, " > ".join(heading_stack)))
        offset += len(line) + 1

    chunks: list[TextChunk] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + size)
        if end < len(normalized):
            lower_bound = start + size // 2
            boundaries = [
                normalized.rfind(separator, lower_bound, end)
                for separator in ("\n", "。", "！", "？", ". ", "! ", "? ")
            ]
            boundary = max(boundaries)
            if boundary >= lower_bound:
                end = boundary + 1
        heading_path = None
        for heading_offset, candidate in headings:
            if heading_offset > start:
                break
            heading_path = candidate
        chunks.append(TextChunk(len(chunks), normalized[start:end], heading_path))
        if end == len(normalized):
            break
        start = end - overlap
    return tuple(chunks)
