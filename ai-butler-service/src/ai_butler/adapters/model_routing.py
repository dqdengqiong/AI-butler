"""静态模型目录、能力约束与启动期校验。"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelProtocol(StrEnum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"


class ThinkingMode(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class OutputMode(StrEnum):
    JSON = "JSON"
    TEXT = "TEXT"
    TOOLS = "TOOLS"


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(min_length=1)
    api_key_ref: str = Field(min_length=1, max_length=64)
    region: str = Field(min_length=1, max_length=64)
    data_residency: str = Field(min_length=1, max_length=32)


class ChatModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    protocol: ModelProtocol
    structured_output: bool
    multimodal: bool
    tools: bool
    context_window_tokens: int = Field(gt=0)
    enabled: bool = True


class EmbeddingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    dimensions: int = Field(gt=0)
    enabled: bool = True


class RouteProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    primary: str = Field(min_length=1, max_length=64)
    fallbacks: tuple[str, ...] = Field(default=(), max_length=1)
    timeout_ms: int = Field(ge=100, le=300_000)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    thinking: ThinkingMode
    output_mode: OutputMode
    requires_multimodal: bool = False
    requires_structured_output: bool = True
    requires_tools: bool = False
    max_attempts: int = Field(default=3, ge=1, le=3)


class ModelRoutingConfig(BaseModel):
    """TOML 的完整类型化表示；未知字段（包括价格）会直接失败。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    providers: dict[str, ProviderProfile]
    models: dict[str, ChatModelProfile]
    embedding: EmbeddingProfile
    routes: dict[str, RouteProfile]

    @model_validator(mode="after")
    def validate_references_and_capabilities(self) -> ModelRoutingConfig:
        if not self.providers:
            raise ValueError("routing must declare at least one provider")
        if not self.models:
            raise ValueError("routing must declare at least one chat model")
        for alias, profile in self.models.items():
            if profile.provider not in self.providers:
                raise ValueError(f"model {alias} references unknown provider")
        if self.embedding.provider not in self.providers:
            raise ValueError("embedding references unknown provider")
        if not self.embedding.enabled:
            raise ValueError("embedding profile must be enabled")
        for task, route in self.routes.items():
            aliases = (route.primary, *route.fallbacks)
            if len(set(aliases)) != len(aliases):
                raise ValueError(f"route {task} contains duplicate model aliases")
            profiles: list[ChatModelProfile] = []
            for alias in aliases:
                candidate = self.models.get(alias)
                if candidate is None:
                    raise ValueError(f"route {task} references unknown model {alias}")
                profile = candidate
                if not profile.enabled:
                    raise ValueError(f"route {task} references disabled model {alias}")
                if route.max_input_tokens + route.max_output_tokens > profile.context_window_tokens:
                    raise ValueError(f"route {task} exceeds context capacity of {alias}")
                if route.requires_multimodal and not profile.multimodal:
                    raise ValueError(f"route {task} requires multimodal model {alias}")
                if route.requires_structured_output and not profile.structured_output:
                    raise ValueError(f"route {task} requires structured output from {alias}")
                if route.requires_tools and not profile.tools:
                    raise ValueError(f"route {task} requires tool support from {alias}")
                profiles.append(profile)
            if len(profiles) == 2 and profiles[0].provider == profiles[1].provider:
                raise ValueError(f"route {task} primary and fallback must use different providers")
            residencies = {self.providers[profile.provider].data_residency for profile in profiles}
            if len(residencies) > 1:
                raise ValueError(f"route {task} models must use the same data residency")
        return self

    def validate_for_environment(self, app_env: str) -> None:
        environment = app_env.lower()
        if environment not in {"evaluation", "eval", "test"} and any(
            "latest" in profile.model.lower() for profile in self.models.values()
        ):
            raise ValueError("latest model aliases are only allowed in evaluation environments")
        if environment not in {"production", "staging"}:
            return
        enabled_providers = {
            profile.provider for profile in self.models.values() if profile.enabled
        }
        if len(enabled_providers) < 2:
            return
        missing = sorted(task for task, route in self.routes.items() if len(route.fallbacks) != 1)
        if missing:
            raise ValueError(
                "production routes require exactly one fallback: " + ", ".join(missing)
            )


def load_model_routing(path: Path, app_env: str) -> ModelRoutingConfig:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid model routing file: {path}") from exc
    routing = ModelRoutingConfig.model_validate(payload)
    routing.validate_for_environment(app_env)
    return routing
