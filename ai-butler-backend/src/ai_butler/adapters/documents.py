"""私有学习资料的受控文本抽取与分块。"""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_RAG_MIME_TYPES = frozenset({"text/plain", "text/markdown", "application/pdf"})


@dataclass(frozen=True, slots=True)
class TextChunk:
    index: int
    content: str


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
    """按字符预算稳定分块；重叠只帮助召回，不改变原文或生成新内容。"""

    normalized = "\n".join(line.strip() for line in value.splitlines() if line.strip())
    if not normalized:
        return ()
    chunks: list[TextChunk] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + size)
        chunks.append(TextChunk(len(chunks), normalized[start:end]))
        if end == len(normalized):
            break
        start = end - overlap
    return tuple(chunks)
