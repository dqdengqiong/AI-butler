"""模型与确定性代码之间的版本化结构化契约。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_butler.agent.availability import AvailabilityInterpretationV1


class ContextItemV1(BaseModel):
    ref: str
    text: str
    trust_level: Literal["SYSTEM_FACT", "USER_CONTENT", "EXTERNAL_UNTRUSTED"]
    estimated_tokens: int = Field(ge=0)


class ContextBundleV1(BaseModel):
    """节点只消费预算内上下文；身份与业务事实不可由模型生成。"""

    schema_version: Literal["1.0"] = "1.0"
    user_id: UUID
    run_id: UUID
    thread_id: str
    current_input: ContextItemV1
    business_facts: tuple[ContextItemV1, ...] = ()
    summaries: tuple[ContextItemV1, ...] = ()
    messages: tuple[ContextItemV1, ...] = ()
    memories: tuple[ContextItemV1, ...] = ()
    evidence: tuple[ContextItemV1, ...] = ()


class ToolLoopStateV1(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    rounds: int = Field(default=0, ge=0, le=2)
    normalized_argument_hashes: tuple[str, ...] = ()
    stopped_reason: Literal["BUDGET", "DUPLICATE", "CANCELLED", "COMPLETE"] | None = None


class AgentStateV1(BaseModel):
    """checkpoint 中只保存单轮 run 的结构化路由状态。"""

    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    user_id: UUID
    conversation_id: UUID
    segment_id: UUID
    thread_id: str
    graph_version: str
    prompt_bundle_version: str
    tool_registry_version: str
    tool_registry_fingerprint: str
    intent: Literal["PLAN", "ADJUST", "QUESTION", "MEMORY", "UNKNOWN"] = "UNKNOWN"
    next_node: Literal[
        "Router",
        "Profile",
        "Research",
        "Planner",
        "Review",
        "Evidence Gate",
        "ToolExecutor",
        "Feedback/Adjust",
        "Response",
    ] = "Router"
    context: ContextBundleV1
    tool_loop: ToolLoopStateV1 = Field(default_factory=ToolLoopStateV1)
    warnings: tuple[str, ...] = ()
    started_at: datetime


class ToolResultMetaV1(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    trust_level: Literal["SYSTEM_FACT", "USER_CONTENT", "EXTERNAL_UNTRUSTED"]
    provenance_refs: tuple[str, ...] = ()
    truncated: bool = False
    next_cursor: str | None = None
    warnings: tuple[str, ...] = ()
    retry_hint: str | None = None


class IntentDecisionV1(BaseModel):
    """运行内业务意图；模型只能选择流程，不能提供实体 ID 或授权。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    intent: Literal[
        "GENERAL_CHAT",
        "CIVIL_QA",
        "DAILY_PLANNING",
        "PLAN_REVIEW",
        "RESEARCH",
        "PLAN_CREATE",
        "PLAN_ADJUST",
        "TASK_FEEDBACK",
        "MEMORY",
        "UNSUPPORTED",
        "CLARIFY",
    ]
    confidence: float = Field(ge=0, le=1)
    context_needs: tuple[
        Literal[
            "PLAN_REQUIREMENTS",
            "PLAN_CONTEXT",
            "TASK_CONTEXT",
            "PUBLIC_KNOWLEDGE",
            "PRIVATE_KNOWLEDGE",
            "MEMORY_COMMAND",
        ],
        ...,
    ] = ()
    clarifying_question: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_clarification(self) -> IntentDecisionV1:
        """低置信度不得静默进入专业流程或产生业务副作用。"""

        if self.intent == "CLARIFY" and not (self.clarifying_question or "").strip():
            raise ValueError("CLARIFY requires a clarifying question")
        if self.intent != "CLARIFY" and self.clarifying_question is not None:
            raise ValueError("clarifying question is only allowed for CLARIFY")
        if len(set(self.context_needs)) != len(self.context_needs):
            raise ValueError("context needs must be unique")
        return self


class FeedbackDecisionV1(BaseModel):
    """任务反馈只决定回复、重新规划或澄清，不携带实体 ID。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    action: Literal["RESPOND", "REPLAN", "CLARIFY"]
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=400)
    clarifying_question: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_clarification(self) -> FeedbackDecisionV1:
        if self.action == "CLARIFY" and not (self.clarifying_question or "").strip():
            raise ValueError("CLARIFY requires a clarifying question")
        if self.action != "CLARIFY" and self.clarifying_question is not None:
            raise ValueError("clarifying question is only allowed for CLARIFY")
        return self


class PlanTaskTemplateV1(BaseModel):
    """Planner 生成的任务模板；具体任务必须在用户确认后物化。"""

    model_config = ConfigDict(extra="forbid")

    template_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    frequency: dict[str, object]
    expected_minutes: int = Field(ge=1, le=1440)
    priority: int = Field(ge=1, le=5)
    claim_keys: tuple[str, ...] = ()


class PlanStageV1(BaseModel):
    """经模型建议、仍需代码复核的连续计划阶段。"""

    model_config = ConfigDict(extra="forbid")

    stage_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=2000)
    sequence: int = Field(ge=1)
    start_date: date
    end_date: date
    allocated_minutes: int = Field(ge=1)
    task_templates: tuple[PlanTaskTemplateV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dates(self) -> PlanStageV1:
        if self.end_date < self.start_date:
            raise ValueError("stage end date must not precede start date")
        return self


class PlanDraftV1(BaseModel):
    """Planner 预览；通过 Review 和用户确认前不写入计划业务表。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    objective_summary: str = Field(min_length=1, max_length=4000)
    start_date: date
    end_date: date
    weekly_minutes: int = Field(ge=1)
    assumptions: tuple[str, ...] = ()
    stages: tuple[PlanStageV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dates(self) -> PlanDraftV1:
        if self.end_date < self.start_date:
            raise ValueError("plan end date must not precede start date")
        return self


class PlannerResultV1(BaseModel):
    """Planner 的完整结构化结果，失败状态不得夹带可持久化计划。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["READY", "NEEDS_INPUT", "INFEASIBLE"]
    plan: PlanDraftV1 | None = None
    question: str | None = Field(default=None, max_length=400)
    adjustment_options: tuple[str, ...] = Field(default=(), max_length=3)
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_status_payload(self) -> PlannerResultV1:
        if self.status == "READY" and self.plan is None:
            raise ValueError("READY requires a plan")
        if self.status != "READY" and self.plan is not None:
            raise ValueError("non-ready planner result cannot include a plan")
        if self.status == "NEEDS_INPUT" and not (self.question or "").strip():
            raise ValueError("NEEDS_INPUT requires a question")
        return self


class PlanScopeDraftV1(BaseModel):
    """用户确认时间后形成的服务端计划范围草稿。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    objective_summary: str = Field(min_length=1, max_length=4000)
    availability: AvailabilityInterpretationV1
    start_date: date


class PlanScopeV1(PlanScopeDraftV1):
    """Planner 唯一可消费的、已由用户确认的目标时间范围。"""

    target_date: date
    period_source: Literal[
        "EXPLICIT_DATE",
        "RELATIVE_DAYS",
        "RELATIVE_WEEKS",
        "RELATIVE_MONTHS",
        "QUICK_WEEKS",
        "CUSTOM_DATE",
    ]

    @model_validator(mode="after")
    def validate_period(self) -> PlanScopeV1:
        if self.target_date <= self.start_date:
            raise ValueError("target_date must be later than start_date")
        return self


class PlannerTemplateSuggestionV2(BaseModel):
    """Planner 只建议学习内容；稳定键、日期与总容量由服务端补齐。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=600)
    days_per_week: int = Field(ge=1, le=7)
    expected_minutes: int = Field(ge=1, le=1440)
    priority: int = Field(ge=1, le=5)
    claim_keys: tuple[str, ...] = ()


class PlannerStageSuggestionV2(BaseModel):
    """对应服务端预先分配阶段窗口的紧凑内容建议。"""

    model_config = ConfigDict(extra="forbid")

    stage_key: str = Field(pattern=r"^stage_[1-4]$")
    name: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=600)
    task_templates: tuple[PlannerTemplateSuggestionV2, ...] = Field(min_length=1, max_length=2)


class PlannerSuggestionV2(BaseModel):
    """紧凑 Planner 输出；失败状态不得携带可持久化阶段建议。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    status: Literal["READY", "NEEDS_INPUT", "INFEASIBLE"]
    title: str | None = Field(default=None, max_length=200)
    objective_summary: str | None = Field(default=None, max_length=1000)
    assumptions: tuple[str, ...] = Field(default=(), max_length=5)
    stages: tuple[PlannerStageSuggestionV2, ...] = Field(default=(), max_length=4)
    question: str | None = Field(default=None, max_length=400)
    adjustment_options: tuple[str, ...] = Field(default=(), max_length=3)
    warnings: tuple[str, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def validate_status_payload(self) -> PlannerSuggestionV2:
        if self.status == "READY":
            if not self.title or not self.objective_summary or not self.stages:
                raise ValueError("READY requires title, objective_summary and stages")
        elif self.stages or self.title is not None or self.objective_summary is not None:
            raise ValueError("non-ready suggestion cannot include plan content")
        if self.status == "NEEDS_INPUT" and not (self.question or "").strip():
            raise ValueError("NEEDS_INPUT requires a question")
        return self


class TaskDraftV1(BaseModel):
    """Executor 产生的七日任务候选；服务端会再次校验日期、容量和幂等键。"""

    model_config = ConfigDict(extra="forbid")

    task_key: str = Field(min_length=1, max_length=160, pattern=r"^[a-z0-9][a-z0-9:_-]*$")
    stage_key: str = Field(min_length=1, max_length=128)
    template_key: str = Field(min_length=1, max_length=128)
    scheduled_date: date
    title: str = Field(min_length=1, max_length=200)
    expected_minutes: int = Field(ge=1, le=1440)
    priority: int = Field(ge=1, le=5)


class ExecutorResultV1(BaseModel):
    """预览阶段的七日任务候选；最终确认前不具有写库授权。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    task_drafts: tuple[TaskDraftV1, ...] = ()
    unscheduled: tuple[dict[str, object], ...] = ()
    warnings: tuple[str, ...] = ()
