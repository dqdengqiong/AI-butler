"""会话延续与新话题判定边界。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol

from ai_butler.adapters.llm import LLM, ModelRequest, ModelResponse, ModelTask


class ConversationRoute(StrEnum):
    CONTINUE = "CONTINUE"
    NEW_TOPIC = "NEW_TOPIC"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class ConversationRouteRequest:
    current_title: str
    recent_messages: tuple[str, ...]
    user_input: str
    idle_seconds: int


@dataclass(frozen=True, slots=True)
class ConversationRouteDecision:
    route: ConversationRoute
    confidence: float
    reason_code: str


class ConversationRouter(Protocol):
    async def route(self, request: ConversationRouteRequest) -> ConversationRouteDecision: ...


class FakeConversationRouter:
    """确定性首版路由器；不保存或记录输入原文。"""

    _continue = re.compile(r"^(继续|接着|刚才|上面|这个计划|再帮我|补充|修改|调整|为什么)")
    _ambiguous = re.compile(r"^(还有一个问题|另外问一下|顺便问问)$")
    _topic_patterns: ClassVar[dict[str, re.Pattern[str]]] = {
        "CIVIL": re.compile(r"国考|省考|公务员|行测|申论|考公"),
        "IELTS": re.compile(r"雅思|IELTS|听力|口语"),
        "JOB": re.compile(r"求职|简历|面试|投递|找工作"),
        "LIFE": re.compile(r"天气|做饭|旅行|电影|睡眠"),
    }

    async def route(self, request: ConversationRouteRequest) -> ConversationRouteDecision:
        value = request.user_input.strip()
        if self._continue.search(value):
            return ConversationRouteDecision(ConversationRoute.CONTINUE, 0.99, "EXPLICIT_FOLLOW_UP")
        if self._ambiguous.fullmatch(value):
            return ConversationRouteDecision(ConversationRoute.AMBIGUOUS, 0.5, "AMBIGUOUS_PHRASE")
        previous = " ".join((request.current_title, *request.recent_messages))
        previous_topics = {
            name for name, pattern in self._topic_patterns.items() if pattern.search(previous)
        }
        next_topics = {
            name for name, pattern in self._topic_patterns.items() if pattern.search(value)
        }
        if previous_topics and next_topics and previous_topics.isdisjoint(next_topics):
            return ConversationRouteDecision(ConversationRoute.NEW_TOPIC, 0.95, "TOPIC_CHANGED")
        if re.search(r"换个话题|开始新话题|不说这个了", value):
            return ConversationRouteDecision(
                ConversationRoute.NEW_TOPIC, 0.99, "EXPLICIT_TOPIC_SWITCH"
            )
        if request.idle_seconds >= 86_400 and not (previous_topics & next_topics):
            return ConversationRouteDecision(ConversationRoute.NEW_TOPIC, 0.9, "IDLE_NEW_INTENT")
        return ConversationRouteDecision(ConversationRoute.CONTINUE, 0.8, "SAME_CONTEXT_DEFAULT")


class LLMConversationRouter:
    """复用配置模型的结构化路由器；解析或上游失败时继续当前会话。"""

    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    async def route(self, request: ConversationRouteRequest) -> ConversationRouteDecision:
        prompt: dict[str, object] = {
            "instruction": (
                "判断新输入是否延续当前话题。仅返回 JSON："
                '{"route":"CONTINUE|NEW_TOPIC|AMBIGUOUS","confidence":0到1,'
                '"reason_code":"不含用户原文的英文枚举"}'
            ),
            "current_title": request.current_title,
            "recent_messages": request.recent_messages,
            "user_input": request.user_input,
            "idle_seconds": request.idle_seconds,
        }
        try:
            response = await self._generate("conversation-router-v1", prompt)
            decision = self._parse(response.content)
            if decision is None:
                repair: dict[str, object] = {
                    "instruction": "只修复为原要求的 JSON，不要解释。",
                    "invalid_output": response.content[:2000],
                    "original_request": prompt,
                }
                response = await self._generate(
                    "conversation-router-v1-repair",
                    repair,
                    model_profile=response.model_profile,
                    attempt_offset=response.attempt,
                )
                decision = self._parse(response.content)
            if decision is None:
                raise ValueError("invalid router response")
            return decision
        except Exception:
            return ConversationRouteDecision(
                ConversationRoute.CONTINUE,
                0.0,
                "ROUTER_UNAVAILABLE_CONTINUE",
            )

    async def _generate(
        self,
        prompt_version: str,
        prompt: dict[str, object],
        *,
        model_profile: str | None = None,
        attempt_offset: int = 0,
    ) -> ModelResponse:
        return await self._llm.generate(
            ModelRequest.user(
                ModelTask.CONVERSATION_ROUTER,
                prompt_version,
                json.dumps(prompt, ensure_ascii=False),
                schema_version="1.0",
                model_profile=model_profile,
                attempt_offset=attempt_offset,
            )
        )

    @staticmethod
    def _parse(content: str) -> ConversationRouteDecision | None:
        try:
            payload = json.loads(content)
            route = ConversationRoute(str(payload["route"]))
            confidence = float(payload["confidence"])
            reason = str(payload["reason_code"])
            if not 0 <= confidence <= 1 or not re.fullmatch(r"[A-Z0-9_]{1,64}", reason):
                return None
            return ConversationRouteDecision(route, confidence, reason)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
