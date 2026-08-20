from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_butler.adapters.llm import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelGateway,
    ModelInvocation,
    ModelRateLimitError,
    ModelRequest,
    ModelResponse,
    ModelServerError,
    ModelTask,
    ModelTimeoutError,
)
from ai_butler.adapters.model_routing import ModelRoutingConfig, load_model_routing
from ai_butler.config import Settings

ROUTING_FILE = Path(__file__).parents[2] / "model-routing.toml"


def _routing_with_doubao(*, referenced: bool = True) -> ModelRoutingConfig:
    payload = tomllib.loads(ROUTING_FILE.read_text(encoding="utf-8"))
    payload["models"]["doubao_turbo"] = {
        "provider": "doubao",
        "model": "doubao-seed-2-1-turbo-260628",
        "protocol": "openai_responses",
        "structured_output": True,
        "multimodal": True,
        "tools": True,
        "context_window_tokens": 256000,
    }
    if referenced:
        payload["routes"]["availability"]["fallbacks"] = ["doubao_turbo"]
    return ModelRoutingConfig.model_validate(payload)


def test_qwen_only_routes_and_embedding_load() -> None:
    routing = load_model_routing(ROUTING_FILE, "production")

    assert set(routing.providers) == {"qwen", "doubao"}
    assert set(routing.models) == {"qwen_balanced"}
    assert routing.models["qwen_balanced"].model == "qwen3.7-plus-2026-05-26"
    assert all(not route.fallbacks for route in routing.routes.values())
    assert routing.routes["multimodal"].primary == "qwen_balanced"
    assert routing.routes["planner"].timeout_ms == 45_000
    assert routing.routes["planner"].max_output_tokens == 4096
    assert routing.routes["planner"].thinking.value == "disabled"
    assert routing.routes["executor"].timeout_ms == 45_000
    assert routing.routes["executor"].max_output_tokens == 4096
    assert routing.routes["executor"].thinking.value == "disabled"
    assert routing.routes["intent_router"].max_input_tokens == 4_000
    assert routing.routes["memory_extractor"].max_input_tokens == 4_000
    assert routing.routes["response"].max_input_tokens == 8_000
    assert routing.routes["planner"].max_input_tokens == 10_000
    assert routing.routes["research"].max_input_tokens == 12_000
    assert routing.embedding.model == "text-embedding-v4"
    assert routing.embedding.dimensions == 1024


def test_shadow_mode_is_explicit_and_never_allowed_in_production() -> None:
    with pytest.raises(ValidationError, match="requires real model routing"):
        Settings(model_routing_enabled=False, model_shadow_mode=True)
    with pytest.raises(ValidationError, match="evaluation environments"):
        Settings(
            app_env="production",
            model_routing_enabled=True,
            model_shadow_mode=True,
        )


def test_model_api_key_placeholder_is_rejected_before_provider_call() -> None:
    with pytest.raises(ValidationError, match="ASCII"):
        Settings(model_api_keys={"doubao": "请填写真实密钥"})


def test_production_requires_real_search_provider() -> None:
    with pytest.raises(ValidationError, match="Tavily"):
        Settings(app_env="production")
    with pytest.raises(ValidationError, match="API key"):
        Settings(search_provider="tavily")


def test_production_requires_fallback_only_when_multiple_model_providers_exist() -> None:
    routing = _routing_with_doubao()

    routing.validate_for_environment("test")
    with pytest.raises(ValueError, match="exactly one fallback"):
        routing.validate_for_environment("production")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.replace(
            "fallbacks = []",
            'fallbacks = ["qwen_balanced"]',
            1,
        ),
        lambda value: value.replace(
            'provider = "qwen"\nmodel = "qwen3.7-plus-2026-05-26"',
            'provider = "missing"\nmodel = "qwen3.7-plus-2026-05-26"',
            1,
        ),
        lambda value: value.replace("multimodal = true", "multimodal = false", 1),
        lambda value: value.replace(
            'model = "qwen3.7-plus-2026-05-26"',
            'model = "qwen3.7-plus-2026-05-26"\nprice_as_of = "2026-08-17"',
            1,
        ),
    ],
)
def test_invalid_reference_capability_and_price_fields_fail(tmp_path: Path, mutate: object) -> None:
    assert callable(mutate)
    path = tmp_path / "invalid.toml"
    path.write_text(mutate(ROUTING_FILE.read_text(encoding="utf-8")), encoding="utf-8")  # type: ignore[operator]
    with pytest.raises((ValueError, ValidationError)):
        load_model_routing(path, "test")


class _Recorder:
    def __init__(self) -> None:
        self.items: list[ModelInvocation] = []

    async def record(self, invocation: ModelInvocation) -> None:
        self.items.append(invocation)


class _Client:
    def __init__(self, alias: str, outcomes: list[Exception | str]) -> None:
        self.alias = alias
        self.outcomes = outcomes
        self.calls = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return ModelResponse(
            provider="qwen" if self.alias == "qwen_balanced" else "doubao",
            model=self.alias,
            model_profile=self.alias,
            content=outcome,
            prompt_version=request.prompt_version,
            input_tokens=10,
            output_tokens=5,
        )


def _gateway(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, list[Exception | str]],
    *,
    shadow_mode: bool = False,
) -> tuple[ModelGateway, dict[str, _Client], _Recorder]:
    clients: dict[str, _Client] = {}

    def factory(_key: str, _url: str, alias: str, _profile: object) -> _Client:
        client = _Client(alias, outcomes[alias])
        clients[alias] = client
        return client

    monkeypatch.setattr("ai_butler.adapters.llm.OpenAICompatibleLLM", factory)
    recorder = _Recorder()
    gateway = ModelGateway(
        _routing_with_doubao(),
        {"qwen": "key", "doubao": "key"},
        recorder,
        shadow_mode=shadow_mode,
    )
    return gateway, clients, recorder


def test_gateway_requires_keys_for_referenced_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_butler.adapters.llm.OpenAICompatibleLLM",
        lambda *_args: _Client("unused", []),
    )
    with pytest.raises(ValueError, match="doubao"):
        ModelGateway(
            _routing_with_doubao(),
            {"qwen": "key"},
        )


def test_gateway_ignores_unreferenced_model_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialized: list[str] = []

    def factory(_key: str, _url: str, alias: str, _profile: object) -> _Client:
        initialized.append(alias)
        return _Client(alias, [])

    monkeypatch.setattr("ai_butler.adapters.llm.OpenAICompatibleLLM", factory)

    ModelGateway(_routing_with_doubao(referenced=False), {"qwen": "key"})

    assert initialized == ["qwen_balanced"]


async def test_primary_success_never_calls_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway, clients, recorder = _gateway(
        monkeypatch,
        {"qwen_balanced": ['{"ok":true}'], "doubao_turbo": ['{"ok":true}']},
    )
    response = await gateway.generate(
        ModelRequest.user(ModelTask.AVAILABILITY, "availability-v1", "input")
    )

    assert response.model_profile == "qwen_balanced"
    assert clients["qwen_balanced"].calls == 1
    assert clients["doubao_turbo"].calls == 0
    assert [item.route_role for item in recorder.items] == ["PRIMARY"]


@pytest.mark.parametrize(
    "error",
    [
        ModelTimeoutError("timeout"),
        ModelConnectionError("connection"),
        ModelRateLimitError("429"),
        ModelServerError("5xx"),
    ],
)
async def test_retryable_primary_error_calls_fallback_once(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    gateway, clients, recorder = _gateway(
        monkeypatch,
        {"qwen_balanced": [error], "doubao_turbo": ['{"ok":true}']},
    )
    response = await gateway.generate(
        ModelRequest.user(ModelTask.AVAILABILITY, "availability-v1", "input")
    )

    assert response.model_profile == "doubao_turbo"
    assert response.attempt == 2
    assert response.fallback_from == "qwen_balanced"
    assert clients["doubao_turbo"].calls == 1
    assert [(item.route_role, item.status) for item in recorder.items] == [
        ("PRIMARY", "FAILED"),
        ("FALLBACK", "SUCCEEDED"),
    ]


async def test_authentication_error_never_calls_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway, clients, _recorder = _gateway(
        monkeypatch,
        {
            "qwen_balanced": [ModelAuthenticationError("auth")],
            "doubao_turbo": ['{"ok":true}'],
        },
    )
    with pytest.raises(ModelAuthenticationError):
        await gateway.generate(
            ModelRequest.user(ModelTask.AVAILABILITY, "availability-v1", "input")
        )
    assert clients["doubao_turbo"].calls == 0


async def test_primary_and_fallback_infrastructure_errors_stop_after_two_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, clients, recorder = _gateway(
        monkeypatch,
        {
            "qwen_balanced": [ModelServerError("5xx")],
            "doubao_turbo": [ModelTimeoutError("timeout")],
        },
    )
    with pytest.raises(ModelTimeoutError):
        await gateway.generate(
            ModelRequest.user(ModelTask.AVAILABILITY, "availability-v1", "input")
        )
    assert clients["qwen_balanced"].calls == clients["doubao_turbo"].calls == 1
    assert [item.status for item in recorder.items] == ["FAILED", "FAILED"]


async def test_schema_repair_stays_on_actual_fallback_and_is_third_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, clients, recorder = _gateway(
        monkeypatch,
        {
            "qwen_balanced": [ModelTimeoutError("timeout")],
            "doubao_turbo": ["not-json", '{"ok":true}'],
        },
    )
    first = await gateway.generate(
        ModelRequest.user(ModelTask.AVAILABILITY, "availability-v1", "input")
    )
    repaired = await gateway.generate(
        ModelRequest.user(
            ModelTask.AVAILABILITY,
            "availability-v1-repair",
            "repair",
            model_profile=first.model_profile,
            attempt_offset=first.attempt,
        )
    )

    assert repaired.model_profile == "doubao_turbo"
    assert repaired.attempt == 3
    assert clients["qwen_balanced"].calls == 1
    assert [item.attempt for item in recorder.items] == [1, 2, 3]


async def test_shadow_call_is_recorded_but_does_not_change_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, _clients, recorder = _gateway(
        monkeypatch,
        {"qwen_balanced": ["primary"], "doubao_turbo": ["candidate"]},
        shadow_mode=True,
    )
    response = await gateway.generate(
        ModelRequest.user(ModelTask.AVAILABILITY, "availability-v1", "input")
    )
    assert response.content == "primary"
    assert [item.route_role for item in recorder.items] == ["PRIMARY", "SHADOW"]
