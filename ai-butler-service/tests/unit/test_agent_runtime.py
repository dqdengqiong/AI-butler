from datetime import UTC, datetime
from uuid import UUID

import pytest

from ai_butler.agent.contracts import ContextBundleV1, ContextItemV1
from ai_butler.agent.runtime import ContextBudgetGuard
from ai_butler.application.butler.memory import MemoryCandidate, MemoryPolicy
from ai_butler.domain.errors import ButlerError
from ai_butler.tools import DEFAULT_TOOL_REGISTRY

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
RUN_ID = UUID("00000000-0000-4000-8000-000000000002")


def _item(ref: str, tokens: int, trust: str = "SYSTEM_FACT") -> ContextItemV1:
    return ContextItemV1.model_validate(
        {"ref": ref, "text": ref, "trust_level": trust, "estimated_tokens": tokens}
    )


def test_context_budget_keeps_required_facts_and_drops_low_priority_evidence() -> None:
    bundle = ContextBundleV1(
        user_id=USER_ID,
        run_id=RUN_ID,
        thread_id="thread",
        current_input=_item("input", 100, "USER_CONTENT"),
        business_facts=(_item("active-plan", 100),),
        messages=(_item("old", 100), _item("new", 100)),
        evidence=(_item("external", 100, "EXTERNAL_UNTRUSTED"),),
    )
    compacted = ContextBudgetGuard(450).compact(bundle)
    assert [item.ref for item in compacted.messages] == ["old", "new"]
    assert compacted.evidence == ()


def test_required_context_fails_closed_when_over_budget() -> None:
    bundle = ContextBundleV1(
        user_id=USER_ID,
        run_id=RUN_ID,
        thread_id="thread",
        current_input=_item("input", 300, "USER_CONTENT"),
        business_facts=(_item("active-plan", 300),),
    )
    with pytest.raises(ButlerError, match="必要上下文"):
        ContextBudgetGuard(512).compact(bundle)
    expanded = ContextBudgetGuard(512, 700).compact(bundle)
    assert expanded.current_input.ref == "input"


def test_tool_gate_allows_only_registered_intent_node_pairs() -> None:
    DEFAULT_TOOL_REGISTRY.require("read_plan_context", "ToolExecutor", "PLAN_REVIEW")
    with pytest.raises(ButlerError) as forbidden:
        DEFAULT_TOOL_REGISTRY.require("read_plan_context", "Response", "PLAN_REVIEW")
    assert forbidden.value.code == "TOOL_FORBIDDEN"
    with pytest.raises(ButlerError):
        DEFAULT_TOOL_REGISTRY.require("read_plan_context", "ToolExecutor", "GENERAL_CHAT")


def test_memory_policy_rejects_sensitive_and_applies_category_ttl() -> None:
    policy = MemoryPolicy()
    accepted = MemoryCandidate(
        normalized_key="study.preference.time",
        value="morning",
        category="PREFERENCE",
        explicit=1,
        stable=1,
        useful=1,
        specific=1,
        repeated=0,
    )
    assert policy.admit(accepted) == (True, 180)
    sensitive = MemoryCandidate(
        normalized_key="identity.secret",
        value="synthetic",
        category="BACKGROUND",
        explicit=1,
        stable=1,
        useful=1,
        specific=1,
        repeated=1,
        sensitive=True,
    )
    assert policy.admit(sensitive) == (False, None)
    assert datetime.now(UTC).tzinfo is UTC
