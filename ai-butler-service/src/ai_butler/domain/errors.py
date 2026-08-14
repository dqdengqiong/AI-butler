"""稳定业务错误；API 层只映射这些安全信息。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ButlerError(Exception):
    """可安全映射到公共错误信封的领域错误。"""

    code: str
    message: str
    status_code: int = 400
    retryable: bool = False
    details: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


def not_found() -> ButlerError:
    return ButlerError("RESOURCE_NOT_FOUND", "资源不存在", 404)


def conflict(code: str, message: str) -> ButlerError:
    return ButlerError(code, message, 409)
