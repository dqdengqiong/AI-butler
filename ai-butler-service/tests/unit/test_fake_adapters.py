from __future__ import annotations

import json

import pytest

from ai_butler.adapters.embedding import FakeEmbeddingProvider
from ai_butler.adapters.llm import (
    FakeLLM,
    FakeScenario,
    ModelRateLimitError,
    ModelRequest,
    ModelServerError,
    ModelTask,
    ModelTimeoutError,
)


async def test_fake_llm_returns_valid_structured_content() -> None:
    response = await FakeLLM().generate(
        ModelRequest.user(ModelTask.CONVERSATION_ROUTER, "router-v1", "hi")
    )
    assert json.loads(response.content) == {"status": "ok"}
    assert response.prompt_version == "router-v1"


async def test_fake_llm_can_return_invalid_json() -> None:
    response = await FakeLLM().generate(
        ModelRequest.user(
            ModelTask.CONVERSATION_ROUTER,
            "router-v1",
            "hi",
            scenario=FakeScenario.INVALID_JSON,
        )
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(response.content)


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        (FakeScenario.TIMEOUT, ModelTimeoutError),
        (FakeScenario.RATE_LIMIT, ModelRateLimitError),
        (FakeScenario.SERVER_ERROR, ModelServerError),
    ],
)
async def test_fake_llm_failure_scenarios(
    scenario: FakeScenario,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        await FakeLLM().generate(
            ModelRequest.user(
                ModelTask.CONVERSATION_ROUTER,
                "router-v1",
                "hi",
                scenario=scenario,
            )
        )


async def test_fake_embedding_is_deterministic() -> None:
    provider = FakeEmbeddingProvider()
    first = await provider.embed("public exam")
    second = await provider.embed("public exam")
    assert first == second
    assert len(first) == provider.dimensions
