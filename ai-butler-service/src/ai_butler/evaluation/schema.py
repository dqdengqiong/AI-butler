from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class EvalSuite(StrEnum):
    SMOKE = "smoke"
    CORE = "core"
    LIVE = "live"
    SECURITY = "security"


class EvalPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class EvalTurnV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["USER", "ASSISTANT", "SYSTEM_EVENT"]
    content: str = Field(min_length=1, max_length=4000)


class EvalFixturesV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence: tuple[str, ...] = ()
    records: dict[str, JsonValue] = Field(default_factory=dict)
    failure_mode: str | None = None


class EvalCitationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str = Field(min_length=1, max_length=128)
    citation_id: str = Field(min_length=1, max_length=128)


class EvalToolCallV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ExpectedToolPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    max_calls: int = Field(default=0, ge=0, le=20)

    @model_validator(mode="after")
    def validate_policy(self) -> ExpectedToolPolicyV1:
        allowed = set(self.allowed)
        required = set(self.required)
        forbidden = set(self.forbidden)
        if len(allowed) != len(self.allowed):
            raise ValueError("allowed tools must be unique")
        if len(required) != len(self.required):
            raise ValueError("required tools must be unique")
        if len(forbidden) != len(self.forbidden):
            raise ValueError("forbidden tools must be unique")
        if not required <= allowed:
            raise ValueError("required tools must also be allowed")
        if allowed & forbidden:
            raise ValueError("allowed and forbidden tools must be disjoint")
        if len(required) > self.max_calls:
            raise ValueError("max_calls cannot be smaller than required tools")
        return self


class ExpectedOutcomeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str = Field(min_length=1, max_length=32)
    state: dict[str, JsonValue] = Field(default_factory=dict)
    citations: tuple[EvalCitationV1, ...] = ()
    tool_policy: ExpectedToolPolicyV1 = Field(default_factory=ExpectedToolPolicyV1)
    side_effects: dict[str, int] = Field(default_factory=dict)
    max_security_violations: int = Field(default=0, ge=0)


class AgentEvalTaskV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(pattern=r"^[A-Z]-\d{2}$")
    name: str = Field(min_length=1, max_length=160)
    input: str = Field(min_length=1, max_length=8000)
    initial_state: dict[str, JsonValue] = Field(default_factory=dict)
    turns: tuple[EvalTurnV1, ...] = ()
    fixtures: EvalFixturesV1 = Field(default_factory=EvalFixturesV1)
    expected_outcome: ExpectedOutcomeV1
    priority: EvalPriority
    suites: tuple[EvalSuite, ...]
    tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_collections(self) -> AgentEvalTaskV1:
        if not self.suites:
            raise ValueError("at least one eval suite is required")
        if len(set(self.suites)) != len(self.suites):
            raise ValueError("eval suites must be unique")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("eval tags must be unique")
        return self


class AgentEvalDatasetV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    dataset_version: str = Field(min_length=1, max_length=32)
    graph_version: str = Field(min_length=1, max_length=32)
    prompt_bundle_version: str = Field(min_length=1, max_length=32)
    tasks: tuple[AgentEvalTaskV1, ...]

    @model_validator(mode="after")
    def validate_task_ids(self) -> AgentEvalDatasetV1:
        task_ids = [task.task_id for task in self.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("eval task IDs must be unique")
        return self


class AgentEvalOutcomeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    status: str = Field(min_length=1, max_length=32)
    output_text: str = Field(default="", max_length=16000)
    state: dict[str, JsonValue] = Field(default_factory=dict)
    citations: tuple[EvalCitationV1, ...] = ()
    tool_calls: tuple[EvalToolCallV1, ...] = ()
    side_effects: dict[str, int] = Field(default_factory=dict)
    security_violations: tuple[str, ...] = ()
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0, ge=0)


class MetricResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    score: float = Field(ge=0, le=1)
    passed: bool
    reason: str


class AgentEvalTrialV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    trial_index: int = Field(ge=1)
    duration_ms: int = Field(ge=0)
    outcome: AgentEvalOutcomeV1
    metrics: tuple[MetricResultV1, ...]
    passed: bool


class EvalReportV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    dataset_version: str
    graph_version: str
    prompt_bundle_version: str
    model: str
    trials_per_task: int = Field(ge=1)
    success_rate: float = Field(ge=0, le=1)
    pass_power_k: float = Field(ge=0, le=1)
    p50_duration_ms: int = Field(ge=0)
    p95_duration_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)
    trials: tuple[AgentEvalTrialV1, ...]


class EvalGateResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    failures: tuple[str, ...] = ()
