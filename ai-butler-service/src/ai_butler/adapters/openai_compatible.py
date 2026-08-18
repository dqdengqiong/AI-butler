"""OpenAI Chat Completions 与 Responses 供应商适配。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from time import perf_counter
from typing import Any, cast

from ai_butler.adapters.model_routing import (
    ChatModelProfile,
    ModelProtocol,
    OutputMode,
    ThinkingMode,
)

from .llm import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelError,
    ModelRateLimitError,
    ModelRequest,
    ModelRequestError,
    ModelResponse,
    ModelSafetyError,
    ModelServerError,
    ModelStreamEvent,
    ModelTimeoutError,
)


class OpenAICompatibleLLM:
    """封装 Chat Completions 与 Responses 差异，禁止 SDK 隐式重试。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        profile_alias: str,
        profile: ChatModelProfile,
    ) -> None:
        if not api_key or not profile.model:
            raise ValueError("LLM credentials and model are required")
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self._profile_alias = profile_alias
        self._profile = profile

    async def generate(self, request: ModelRequest) -> ModelResponse:
        started = perf_counter()
        try:
            if self._profile.protocol is ModelProtocol.OPENAI_RESPONSES:
                content, usage, actual_model = await self._responses(request)
            else:
                content, usage, actual_model = await self._chat(request)
        except Exception as exc:
            raise _classify_provider_error(exc) from exc
        if not content:
            raise ModelServerError("model returned empty content")
        return ModelResponse(
            provider=self._profile.provider,
            model=actual_model,
            model_profile=self._profile_alias,
            content=content,
            prompt_version=request.prompt_version,
            input_tokens=usage[0],
            cached_input_tokens=usage[1],
            output_tokens=usage[2],
            duration_ms=round((perf_counter() - started) * 1000),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """直接转发供应商文本增量，并在末尾补齐可审计的用量元数据。"""

        started = perf_counter()
        try:
            if self._profile.protocol is ModelProtocol.OPENAI_RESPONSES:
                iterator = self._responses_stream(request)
            else:
                iterator = self._chat_stream(request)
            content_parts: list[str] = []
            final: tuple[tuple[int, int, int], str] | None = None
            async for delta, metadata in iterator:
                if delta:
                    content_parts.append(delta)
                    yield ModelStreamEvent(delta=delta)
                if metadata is not None:
                    final = metadata
        except Exception as exc:
            raise _classify_provider_error(exc) from exc
        content = "".join(content_parts)
        if not content or final is None:
            raise ModelServerError("model returned incomplete stream")
        usage, actual_model = final
        yield ModelStreamEvent(
            response=ModelResponse(
                provider=self._profile.provider,
                model=actual_model,
                model_profile=self._profile_alias,
                content=content,
                prompt_version=request.prompt_version,
                input_tokens=usage[0],
                cached_input_tokens=usage[1],
                output_tokens=usage[2],
                duration_ms=round((perf_counter() - started) * 1000),
            )
        )

    async def _chat_stream(
        self, request: ModelRequest
    ) -> AsyncIterator[tuple[str, tuple[tuple[int, int, int], str] | None]]:
        """适配 Chat Completions 增量；最后一个 chunk 提供 usage。"""

        extra_body: dict[str, object] = {}
        if self._profile.provider == "qwen":
            extra_body["enable_thinking"] = request.thinking is ThinkingMode.ENABLED
        create = cast(Any, self._client.chat.completions.create)
        stream = await create(
            model=self._profile.model,
            messages=[
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            temperature=0,
            max_tokens=request.max_output_tokens,
            timeout=(request.timeout_ms or 10_000) / 1000,
            response_format={"type": "json_object"}
            if request.output_mode is OutputMode.JSON
            else None,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=extra_body or None,
        )
        usage: tuple[int, int, int] = (0, 0, 0)
        actual_model = self._profile.model
        async for chunk in stream:
            actual_model = str(getattr(chunk, "model", None) or actual_model)
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                cached = (
                    getattr(getattr(chunk_usage, "prompt_tokens_details", None), "cached_tokens", 0)
                    or 0
                )
                usage = (
                    getattr(chunk_usage, "prompt_tokens", 0) or 0,
                    cached,
                    getattr(chunk_usage, "completion_tokens", 0) or 0,
                )
            choices = getattr(chunk, "choices", ()) or ()
            if choices and getattr(choices[0], "finish_reason", None) == "content_filter":
                raise ModelSafetyError("model safety refusal")
            delta = (
                str(getattr(getattr(choices[0], "delta", None), "content", "") or "")
                if choices
                else ""
            )
            if delta:
                yield delta, None
        yield "", (usage, actual_model)

    async def _responses_stream(
        self, request: ModelRequest
    ) -> AsyncIterator[tuple[str, tuple[tuple[int, int, int], str] | None]]:
        """适配 Responses 增量事件；未知事件只作为供应商内部元数据忽略。"""

        completion = await self._client.responses.create(
            model=self._profile.model,
            input=[
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            max_output_tokens=request.max_output_tokens,
            timeout=(request.timeout_ms or 10_000) / 1000,
            stream=True,
            extra_body={
                "thinking": {
                    "type": "enabled" if request.thinking is ThinkingMode.ENABLED else "disabled"
                }
            },
        )
        usage: tuple[int, int, int] = (0, 0, 0)
        actual_model = self._profile.model
        async for event in completion:
            event_type = str(getattr(event, "type", ""))
            if event_type == "response.output_text.delta":
                delta = str(getattr(event, "delta", "") or "")
                if delta:
                    yield delta, None
            elif event_type == "response.completed":
                response = getattr(event, "response", None)
                actual_model = str(getattr(response, "model", None) or actual_model)
                response_usage = getattr(response, "usage", None)
                cached = (
                    getattr(
                        getattr(response_usage, "input_tokens_details", None),
                        "cached_tokens",
                        0,
                    )
                    or 0
                )
                usage = (
                    getattr(response_usage, "input_tokens", 0) or 0,
                    cached,
                    getattr(response_usage, "output_tokens", 0) or 0,
                )
            elif event_type in {"response.failed", "response.incomplete"}:
                raise ModelServerError("model returned incomplete stream")
        yield "", (usage, actual_model)

    async def _chat(self, request: ModelRequest) -> tuple[str | None, tuple[int, int, int], str]:
        extra_body: dict[str, object] = {}
        if self._profile.provider == "qwen":
            extra_body["enable_thinking"] = request.thinking is ThinkingMode.ENABLED
        create = cast(Any, self._client.chat.completions.create)
        completion = await create(
            model=self._profile.model,
            messages=[
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            temperature=0,
            max_tokens=request.max_output_tokens,
            timeout=(request.timeout_ms or 10_000) / 1000,
            response_format={"type": "json_object"}
            if request.output_mode is OutputMode.JSON
            else None,
            extra_body=extra_body or None,
        )
        if getattr(completion.choices[0], "finish_reason", None) == "content_filter":
            raise ModelSafetyError("model safety refusal")
        usage = getattr(completion, "usage", None)
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        return (
            completion.choices[0].message.content,
            (
                getattr(usage, "prompt_tokens", 0) or 0,
                cached,
                getattr(usage, "completion_tokens", 0) or 0,
            ),
            str(getattr(completion, "model", None) or self._profile.model),
        )

    async def _responses(
        self, request: ModelRequest
    ) -> tuple[str | None, tuple[int, int, int], str]:
        extra_body = {
            "thinking": {
                "type": "enabled" if request.thinking is ThinkingMode.ENABLED else "disabled"
            }
        }
        completion = await self._client.responses.create(
            model=self._profile.model,
            input=[
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            max_output_tokens=request.max_output_tokens,
            timeout=(request.timeout_ms or 10_000) / 1000,
            extra_body=extra_body,
        )
        if getattr(getattr(completion, "incomplete_details", None), "reason", None) in {
            "content_filter",
            "safety",
        }:
            raise ModelSafetyError("model safety refusal")
        usage = getattr(completion, "usage", None)
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        return (
            getattr(completion, "output_text", None),
            (
                getattr(usage, "input_tokens", 0) or 0,
                cached,
                getattr(usage, "output_tokens", 0) or 0,
            ),
            str(getattr(completion, "model", None) or self._profile.model),
        )


def _classify_provider_error(exc: Exception) -> ModelError:
    """只返回稳定分类，不泄露可能包含密钥或用户内容的上游异常。"""

    if isinstance(exc, ModelError):
        return exc
    if isinstance(exc, TimeoutError):
        return ModelTimeoutError("model request timed out")
    try:
        import openai

        if isinstance(exc, openai.APITimeoutError):
            return ModelTimeoutError("model request timed out")
        if isinstance(exc, openai.APIConnectionError):
            return ModelConnectionError("model connection failed")
        if isinstance(exc, openai.RateLimitError):
            return ModelRateLimitError("model rate limited")
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return ModelAuthenticationError("model authentication failed")
        if isinstance(exc, openai.BadRequestError):
            return ModelRequestError("model request rejected")
        if isinstance(exc, openai.APIStatusError) and exc.status_code >= 500:
            return ModelServerError("model provider unavailable")
        if isinstance(exc, openai.APIStatusError):
            return ModelRequestError("model request rejected")
    except (ImportError, AttributeError):
        pass
    return ModelServerError("model provider request failed")
