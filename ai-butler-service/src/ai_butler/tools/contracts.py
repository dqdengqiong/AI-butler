"""公共工具使用的版本化输入输出契约。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_butler.agent.availability import AvailabilityInterpretationV1


class PlanningScenarioSpec(BaseModel):
    """声明一个可复用计划场景需要收集的业务字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=64)
    required_fields: tuple[str, ...]
    field_prompts: dict[str, str] = Field(default_factory=dict)
    field_patterns: dict[str, str] = Field(default_factory=dict)
    default_fields: dict[str, str] = Field(default_factory=dict)
    retrieval_query_prefix: str = ""


class PlanRequirementsV1(BaseModel):
    """完成澄清后可供预览工具消费的规范化计划要求。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    scenario_code: str
    objective_summary: str = Field(min_length=1, max_length=4000)
    start_date: date
    target_date: date
    period_source: Literal["EXPLICIT_DATE", "RELATIVE_DAYS", "RELATIVE_WEEKS", "RELATIVE_MONTHS"]
    availability: AvailabilityInterpretationV1
    scenario_fields: dict[str, str]
    target_plan_id: str | None = None
    expected_current_revision_id: str | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> PlanRequirementsV1:
        if self.target_date <= self.start_date:
            raise ValueError("target_date must be later than start_date")
        if self.availability.status != "COMPLETE" or not self.availability.weekly_minutes:
            raise ValueError("availability must be complete")
        return self


class ToolResultV1(BaseModel):
    """所有公共工具共享的外层结果。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["COMPLETED", "NEEDS_CLARIFICATION"]
    data: PlanRequirementsV1 | dict[str, object] | None = None
    clarification: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_payload(self) -> ToolResultV1:
        if self.status == "COMPLETED" and self.data is None:
            raise ValueError("completed tool result requires data")
        if self.status == "NEEDS_CLARIFICATION" and not (self.clarification or "").strip():
            raise ValueError("clarification result requires a question")
        return self


class ScheduledTaskV1(BaseModel):
    """确定性排期产生的单个任务候选。"""

    model_config = ConfigDict(extra="forbid")

    task_key: str
    stage_key: str
    template_key: str
    title: str
    scheduled_date: date
    expected_minutes: int = Field(gt=0)
    priority: int = Field(ge=1, le=5)
