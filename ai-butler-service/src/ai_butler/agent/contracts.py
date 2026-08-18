"""模型与确定性代码之间的版本化结构化契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ContextItemV1(BaseModel):
    ref: str
    text: str
    trust_level: Literal["SYSTEM_FACT", "USER_CONTENT", "EXTERNAL_UNTRUSTED"]
    estimated_tokens: int = Field(ge=0)


class ContextBundleV1(BaseModel):
    """节点只消费预算内上下文；身份与审批事实不可由模型生成。"""

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
    """checkpoint 中只保存恢复所需状态，不代替 PostgreSQL 业务事实。"""

    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    user_id: UUID
    conversation_id: UUID
    segment_id: UUID
    thread_id: str
    pending_action_key: str
    graph_version: str
    prompt_bundle_version: str
    capability_registry_version: str
    capability_registry_fingerprint: str
    intent: Literal["PLAN", "ADJUST", "QUESTION", "MEMORY", "UNKNOWN"] = "UNKNOWN"
    next_node: Literal[
        "Router",
        "Profile",
        "Research",
        "Planner",
        "Review",
        "Evidence Gate",
        "Approval",
        "Executor",
        "Feedback/Adjust",
        "Response",
    ] = "Router"
    context: ContextBundleV1
    tool_loop: ToolLoopStateV1 = Field(default_factory=ToolLoopStateV1)
    warnings: tuple[str, ...] = ()
    started_at: datetime


class CapabilityResultMetaV1(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    trust_level: Literal["SYSTEM_FACT", "USER_CONTENT", "EXTERNAL_UNTRUSTED"]
    provenance_refs: tuple[str, ...] = ()
    truncated: bool = False
    next_cursor: str | None = None
    warnings: tuple[str, ...] = ()
    retry_hint: str | None = None
