"""Agent 模型节点的稳定领域错误映射。"""

from ai_butler.adapters.llm import ModelError, RetryableModelError
from ai_butler.domain.errors import ButlerError


def model_boundary_error(exc: ModelError, code_prefix: str, operation: str) -> ButlerError:
    """把供应商错误转换为领域错误，保留超时、限流和 5xx 的重试语义。"""

    retryable = isinstance(exc, RetryableModelError)
    suffix = "UNAVAILABLE" if retryable else "INVALID"
    message = f"{operation}暂时不可用，请稍后重试" if retryable else f"{operation}失败"
    return ButlerError(
        f"{code_prefix}_MODEL_{suffix}", message, 503 if retryable else 502, retryable
    )
