"""Agent 公共工具的唯一注册表与代码白名单路由。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from ai_butler.domain.errors import ButlerError

ToolEffect = Literal["READ_ONLY", "CHAT_ONLY", "USER_MUTATION"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    allowed_intents: frozenset[str]
    allowed_nodes: frozenset[str]
    effect: ToolEffect
    model_visible: bool = False


class ToolRegistry:
    """集中维护工具权限，并将语义需求解析为固定工具调用计划。"""

    def __init__(self, specs: tuple[ToolSpec, ...]) -> None:
        if len({spec.name for spec in specs}) != len(specs):
            raise ValueError("tool names must be unique")
        self._specs = {spec.name: spec for spec in specs}
        canonical = [
            {
                "name": item.name,
                "intents": sorted(item.allowed_intents),
                "nodes": sorted(item.allowed_nodes),
                "effect": item.effect,
                "model_visible": item.model_visible,
            }
            for item in sorted(specs, key=lambda item: item.name)
        ]
        self.fingerprint = hashlib.sha256(
            json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()

    def require(self, tool: str, node: str, intent: str | None = None) -> ToolSpec:
        spec = self._specs.get(tool)
        if spec is None or node not in spec.allowed_nodes:
            raise ButlerError("TOOL_FORBIDDEN", "当前节点无权调用该工具", 403)
        if intent is not None and intent not in spec.allowed_intents:
            raise ButlerError("TOOL_FORBIDDEN", "当前意图无权调用该工具", 403)
        return spec

    def resolve(self, intent: str, context_needs: tuple[str, ...]) -> tuple[str, ...]:
        needs = set(context_needs)
        tools: list[str] = []
        if intent in {"PLAN_CREATE", "PLAN_ADJUST"}:
            tools.append("collect_plan_requirements")
        if "PLAN_CONTEXT" in needs:
            tools.append("read_plan_context")
        if "TASK_CONTEXT" in needs:
            tools.append("read_task_context")
        if "PUBLIC_KNOWLEDGE" in needs:
            tools.append("search_public_knowledge")
        if "PRIVATE_KNOWLEDGE" in needs:
            tools.append("search_private_knowledge")
        if "MEMORY_COMMAND" in needs:
            tools.append("handle_memory_command")
        for name in tools:
            self.require(name, "ToolExecutor", intent)
        return tuple(tools)


DEFAULT_TOOL_REGISTRY = ToolRegistry(
    (
        ToolSpec(
            "collect_plan_requirements",
            frozenset({"PLAN_CREATE", "PLAN_ADJUST"}),
            frozenset({"ToolExecutor"}),
            "READ_ONLY",
        ),
        ToolSpec(
            "prepare_plan_preview",
            frozenset({"PLAN_CREATE", "PLAN_ADJUST"}),
            frozenset({"ToolExecutor"}),
            "CHAT_ONLY",
        ),
        ToolSpec(
            "schedule_plan_window",
            frozenset({"PLAN_CREATE", "PLAN_ADJUST"}),
            frozenset({"ToolExecutor", "Scheduler"}),
            "READ_ONLY",
        ),
        ToolSpec(
            "read_plan_context",
            frozenset({"DAILY_PLANNING", "PLAN_REVIEW", "PLAN_ADJUST", "TASK_FEEDBACK"}),
            frozenset({"ToolExecutor"}),
            "READ_ONLY",
        ),
        ToolSpec(
            "read_task_context",
            frozenset({"DAILY_PLANNING", "PLAN_REVIEW", "TASK_FEEDBACK"}),
            frozenset({"ToolExecutor"}),
            "READ_ONLY",
        ),
        ToolSpec(
            "search_public_knowledge",
            frozenset({"RESEARCH", "CIVIL_QA", "PLAN_CREATE", "PLAN_ADJUST"}),
            frozenset({"ToolExecutor"}),
            "READ_ONLY",
        ),
        ToolSpec(
            "search_private_knowledge",
            frozenset({"RESEARCH", "CIVIL_QA", "GENERAL_CHAT"}),
            frozenset({"ToolExecutor"}),
            "READ_ONLY",
        ),
        ToolSpec(
            "handle_memory_command",
            frozenset({"MEMORY"}),
            frozenset({"ToolExecutor"}),
            "USER_MUTATION",
        ),
    )
)
