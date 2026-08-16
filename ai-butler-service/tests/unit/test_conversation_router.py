from __future__ import annotations

import pytest

from ai_butler.adapters.conversation_router import (
    ConversationRoute,
    ConversationRouteRequest,
    FakeConversationRouter,
    LLMConversationRouter,
)
from ai_butler.adapters.llm import ModelRequest, ModelResponse


def _request(value: str, *, idle_seconds: int = 0) -> ConversationRouteRequest:
    return ConversationRouteRequest(
        current_title="公务员备考",
        recent_messages=("正在制定行测计划",),
        user_input=value,
        idle_seconds=idle_seconds,
    )


@pytest.mark.parametrize("value", ["继续完善", "刚才那个计划", "修改一下"])
async def test_fake_router_continues_explicit_follow_up(value: str) -> None:
    decision = await FakeConversationRouter().route(_request(value))
    assert decision.route is ConversationRoute.CONTINUE
    assert decision.confidence >= 0.85


async def test_fake_router_handles_topic_change_ambiguity_and_idle() -> None:
    router = FakeConversationRouter()
    changed = await router.route(_request("帮我修改求职简历"))
    ambiguous = await router.route(_request("另外问一下"))
    idle = await router.route(_request("推荐一部电影", idle_seconds=86_400))
    default = await router.route(_request("再给一些建议"))

    assert changed.route is ConversationRoute.NEW_TOPIC
    assert changed.reason_code == "TOPIC_CHANGED"
    assert ambiguous.route is ConversationRoute.AMBIGUOUS
    assert idle.route is ConversationRoute.NEW_TOPIC
    assert default.route is ConversationRoute.CONTINUE


class _LLM:
    def __init__(self, content: str | None = None, *, fails: bool = False) -> None:
        self.content = content
        self.fails = fails

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if self.fails:
            raise RuntimeError("upstream included private input")
        return ModelResponse("test", self.content or "", request.prompt_version)


async def test_llm_router_parses_safe_decision_and_fails_safe() -> None:
    routed = await LLMConversationRouter(
        _LLM('{"route":"NEW_TOPIC","confidence":0.91,"reason_code":"GOAL_CHANGED"}')
    ).route(_request("新目标"))
    invalid = await LLMConversationRouter(_LLM("not-json")).route(_request("私密内容"))
    failed = await LLMConversationRouter(_LLM(fails=True)).route(_request("私密内容"))

    assert routed.route is ConversationRoute.NEW_TOPIC
    assert routed.reason_code == "GOAL_CHANGED"
    assert invalid.route is ConversationRoute.CONTINUE
    assert invalid.reason_code == "ROUTER_UNAVAILABLE_CONTINUE"
    assert failed == invalid
