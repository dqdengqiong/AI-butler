from __future__ import annotations

from types import SimpleNamespace

import httpx
import openai
import pytest

from ai_butler.adapters.auth import WechatCodeAuthProvider
from ai_butler.adapters.embedding import OpenAICompatibleEmbeddingProvider
from ai_butler.adapters.llm import (
    ModelRequest,
    ModelServerError,
    ModelTimeoutError,
    OpenAICompatibleLLM,
)


class _WechatClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def __aenter__(self) -> _WechatClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, *_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=self.payload, request=httpx.Request("GET", "https://wx"))

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=self.payload, request=httpx.Request("POST", "https://wx"))


class _FailingWechatClient(_WechatClient):
    async def get(self, *_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(
            502,
            request=httpx.Request("GET", "https://wx/?secret=must-not-escape"),
        )


async def test_wechat_code_exchange_and_invalid_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="credentials"):
        WechatCodeAuthProvider("", "")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _WechatClient({"openid": "openid"}))
    identity = await WechatCodeAuthProvider("app", "secret").exchange("code")
    assert identity.provider == "WECHAT_MINIAPP"
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _WechatClient({"errcode": 1}))
    with pytest.raises(ValueError, match="exchange failed"):
        await WechatCodeAuthProvider("app", "secret").exchange("bad")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _FailingWechatClient({}))
    with pytest.raises(ValueError, match="exchange failed") as error:
        await WechatCodeAuthProvider("app", "secret").exchange("secret-login-code")
    assert error.value.__cause__ is None
    assert "must-not-escape" not in str(error.value)


async def test_wechat_phone_exchange_reuses_application_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Client(_WechatClient):
        async def post(self, url: str, **_kwargs: object) -> httpx.Response:
            calls.append(url)
            payload: dict[str, object]
            if url.endswith("stable_token"):
                payload = {"access_token": "upstream-secret-token", "expires_in": 7200}
            else:
                payload = {
                    "errcode": 0,
                    "phone_info": {"purePhoneNumber": "13800138000"},
                }
            return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _Client({}))
    provider = WechatCodeAuthProvider("app", "secret")
    assert await provider.exchange_phone("phone-code-1") == "13800138000"
    assert await provider.exchange_phone("phone-code-2") == "13800138000"
    assert sum(url.endswith("stable_token") for url in calls) == 1
    assert sum(url.endswith("getuserphonenumber") for url in calls) == 2


class _OpenAIClient:
    def __init__(
        self, *, content: str | None = '{"ok":true}', error: Exception | None = None
    ) -> None:
        async def create_chat(**_kwargs: object) -> object:
            if error:
                raise error
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

        async def create_embedding(**_kwargs: object) -> object:
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])])

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create_chat))
        self.embeddings = SimpleNamespace(create=create_embedding)


async def test_openai_compatible_chat_and_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _OpenAIClient()
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_kwargs: client)
    llm = OpenAICompatibleLLM("key", "https://provider", "model")
    response = await llm.generate(ModelRequest(prompt_version="v1", user_input="input"))
    assert response.content == '{"ok":true}'
    embedding = OpenAICompatibleEmbeddingProvider("key", "https://provider", "embed")
    assert await embedding.embed("input") == [0.1, 0.2]
    with pytest.raises(ValueError, match="credentials"):
        OpenAICompatibleLLM("", "", "")
    with pytest.raises(ValueError, match="credentials"):
        OpenAICompatibleEmbeddingProvider("", "", "")


@pytest.mark.parametrize(
    ("error", "expected"),
    [(TimeoutError(), ModelTimeoutError), (RuntimeError(), ModelServerError)],
)
async def test_openai_compatible_sanitizes_provider_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: type[Exception]
) -> None:
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_kwargs: _OpenAIClient(error=error))
    with pytest.raises(expected):
        await OpenAICompatibleLLM("key", "", "model").generate(
            ModelRequest(prompt_version="v1", user_input="secret")
        )


async def test_openai_compatible_rejects_empty_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_kwargs: _OpenAIClient(content=None))
    with pytest.raises(ModelServerError, match="empty"):
        await OpenAICompatibleLLM("key", "", "model").generate(
            ModelRequest(prompt_version="v1", user_input="input")
        )
