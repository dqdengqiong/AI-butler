"""能力注册、权限门和上下文/记忆确定性策略。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from ai_butler.agent.contracts import ContextBundleV1, ContextItemV1
from ai_butler.domain.errors import ButlerError


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    name: str
    allowed_nodes: frozenset[str]
    side_effect: Literal["NONE", "LOW", "HIGH"]
    model_visible: bool = False


class CapabilityRegistry:
    """不可变能力注册表；验证版模型看不到任何 Action 能力。"""

    def __init__(self, specs: tuple[CapabilitySpec, ...]) -> None:
        if len({spec.name for spec in specs}) != len(specs):
            raise ValueError("capability names must be unique")
        self._specs = {spec.name: spec for spec in specs}
        canonical = [
            {
                "name": item.name,
                "nodes": sorted(item.allowed_nodes),
                "side_effect": item.side_effect,
                "model_visible": item.model_visible,
            }
            for item in sorted(specs, key=lambda item: item.name)
        ]
        self.fingerprint = hashlib.sha256(
            json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()

    def require(self, capability: str, node: str, *, approved: bool, replay: bool = False) -> None:
        spec = self._specs.get(capability)
        if spec is None or node not in spec.allowed_nodes:
            raise ButlerError("CAPABILITY_FORBIDDEN", "当前节点无权调用该能力", 403)
        if replay and spec.side_effect != "NONE":
            raise ButlerError("REPLAY_READ_ONLY", "回放模式禁止业务写入", 403)
        if spec.side_effect == "HIGH" and not approved:
            raise ButlerError("APPROVAL_REQUIRED", "该操作需要结构化审批", 409)


class ContextBudgetGuard:
    """按固定优先级裁剪外部证据、记忆和旧消息，保留身份与当前输入。"""

    def __init__(self, max_tokens: int) -> None:
        if max_tokens < 256:
            raise ValueError("context budget is too small")
        self.max_tokens = max_tokens

    def compact(self, bundle: ContextBundleV1) -> ContextBundleV1:
        required = bundle.current_input.estimated_tokens + sum(
            item.estimated_tokens for item in bundle.business_facts
        )
        if required > self.max_tokens:
            raise ButlerError("CONTEXT_REQUIRED_OVER_BUDGET", "必要上下文超过模型预算", 422)
        remaining = self.max_tokens - required
        selected: dict[str, tuple[ContextItemV1, ...]] = {}
        for field, items in (
            ("summaries", bundle.summaries),
            ("messages", tuple(reversed(bundle.messages))),
            ("memories", bundle.memories),
            ("evidence", bundle.evidence),
        ):
            kept: list[ContextItemV1] = []
            for item in items:
                if item.estimated_tokens <= remaining:
                    kept.append(item)
                    remaining -= item.estimated_tokens
            selected[field] = tuple(reversed(kept)) if field == "messages" else tuple(kept)
        return bundle.model_copy(update=selected)


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    normalized_key: str
    value: str
    category: Literal["PREFERENCE", "HABIT", "CONSTRAINT", "BACKGROUND"]
    explicit: float
    stable: float
    useful: float
    specific: float
    repeated: float
    user_requested: bool = False
    sensitive: bool = False


class MemoryPolicy:
    """模型只能提出候选；确定性 Policy 决定准入与 TTL。"""

    @staticmethod
    def score(candidate: MemoryCandidate) -> float:
        return (
            0.35 * candidate.explicit
            + 0.25 * candidate.stable
            + 0.20 * candidate.useful
            + 0.10 * candidate.specific
            + 0.10 * candidate.repeated
        )

    def admit(self, candidate: MemoryCandidate) -> tuple[bool, int | None]:
        if candidate.sensitive or not candidate.normalized_key or not candidate.value:
            return False, None
        threshold = 0.60 if candidate.user_requested else 0.75
        if self.score(candidate) < threshold:
            return False, None
        ttl = 180 if candidate.category in {"PREFERENCE", "HABIT"} else 365
        return True, ttl


DEFAULT_CAPABILITY_REGISTRY = CapabilityRegistry(
    (
        CapabilitySpec("context_load", frozenset({"Profile", "Planner", "Response"}), "NONE"),
        CapabilitySpec("research_collect_evidence", frozenset({"Research"}), "NONE"),
        CapabilitySpec("plan_draft_write", frozenset({"Planner"}), "LOW"),
        CapabilitySpec("plan_publish", frozenset({"Executor"}), "HIGH"),
        CapabilitySpec("task_materialize", frozenset({"Executor"}), "HIGH"),
    )
)
