"""应用服务的响应构造、Provider 工厂和确定性纯规则。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from ai_butler.adapters.embedding import (
    EmbeddingProvider,
    FakeEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)
from ai_butler.adapters.llm import LLM, FakeLLM, OpenAICompatibleLLM
from ai_butler.adapters.search import FakeSearchProvider, SearchProvider, TavilySearchProvider
from ai_butler.agent.availability import AvailabilityInterpretationV1
from ai_butler.api.schemas import AvailabilityRequest
from ai_butler.config import Settings
from ai_butler.domain.errors import ButlerError
from ai_butler.security import issue_access_token, issue_signed_ticket


class ResponseFactory:
    """集中构造带签名或跨模块共享的稳定应用层响应。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def token(
        self,
        user: dict[str, object],
        session_id: UUID,
        refresh_token: str,
        is_new: bool,
    ) -> dict[str, object]:
        user = {**user, "is_new_user": is_new}
        return {
            "access_token": issue_access_token(
                UUID(str(user["id"])),
                session_id,
                self.settings.auth_access_token_secret,
                self.settings.auth_access_token_seconds,
            ),
            "token_type": "Bearer",
            "expires_in": self.settings.auth_access_token_seconds,
            "refresh_token": refresh_token,
            "refresh_expires_in": self.settings.auth_refresh_token_seconds,
            "user": user,
        }

    def send(self, conversation: dict[str, object], run: dict[str, object]) -> dict[str, object]:
        run_id = UUID(str(run["run_id"]))
        return {
            "schema_version": "1.0",
            "conversation_id": conversation["id"],
            "user_message": {"id": run["user_message_id"], "status": "COMPLETED"},
            "assistant_message": {"id": run["response_message_id"], "status": "PENDING"},
            "run": {
                "id": run_id,
                "status": run["status"],
                "execution_mode": "START",
                "attempt": run["attempt"],
            },
            "stream": {
                "events_url": f"/v1/agent-runs/{run_id}/events",
                "ticket": issue_signed_ticket(
                    run_id,
                    self.settings.stream_ticket_secret,
                    self.settings.stream_ticket_seconds,
                ),
                "expires_at": datetime.now(UTC)
                + timedelta(seconds=self.settings.stream_ticket_seconds),
                "last_sequence": 1,
            },
        }

    # 能力模块保留原私有方法名，门面迁移期间无需改写业务调用点。
    _token_response = token
    _send_response = send


def build_search_provider(settings: Settings) -> SearchProvider:
    if settings.search_provider == "fake":
        return FakeSearchProvider()
    if settings.search_provider == "tavily":
        return TavilySearchProvider(
            settings.tavily_api_key,
            settings.tavily_base_url,
            settings.search_timeout_seconds,
        )
    raise ValueError(f"unsupported search provider: {settings.search_provider}")


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_model == "fake-embedding-v1":
        return FakeEmbeddingProvider()
    return OpenAICompatibleEmbeddingProvider(
        settings.llm_api_key,
        settings.llm_base_url,
        settings.embedding_model,
    )


def build_llm(settings: Settings) -> LLM:
    if settings.llm_provider == "fake":
        return FakeLLM(settings.chat_model)
    if settings.llm_provider == "openai-compatible":
        return OpenAICompatibleLLM(
            settings.llm_api_key,
            settings.llm_base_url,
            settings.chat_model,
        )
    raise ValueError(f"unsupported llm provider: {settings.llm_provider}")


def safe_summary(content: str) -> str:
    normalized = " ".join(content.split())
    return f"用户提交了 {len(normalized)} 个字符的请求"


def draft_tasks_for_availability(
    start: date, availability: AvailabilityInterpretationV1
) -> list[dict[str, object]]:
    capacity_by_day: dict[int, int] = {}
    for window in availability.windows:
        capacity_by_day[window.day_of_week] = (
            capacity_by_day.get(window.day_of_week, 0) + window.available_minutes
        )
    allowed_days = (
        set(capacity_by_day)
        if capacity_by_day
        else set(range(1, 8)) - set(availability.excluded_days)
    )
    templates = (("行测基础摸底", 40), ("申论素材精读", 30), ("错题复盘", 35))
    tasks: list[dict[str, object]] = []
    next_offset = 0
    for title, expected_minutes in templates:
        while (
            next_offset <= 27
            and (start + timedelta(days=next_offset)).isoweekday() not in allowed_days
        ):
            next_offset += 1
        if next_offset > 27:
            break
        weekday = (start + timedelta(days=next_offset)).isoweekday()
        available_minutes = capacity_by_day.get(weekday, expected_minutes)
        tasks.append(
            {
                "title": title,
                "day_offset": next_offset,
                "minutes": min(expected_minutes, available_minutes),
            }
        )
        next_offset += 1
    return tasks


def validate_availability_overlap(request: AvailabilityRequest) -> None:
    for index, left in enumerate(request.windows):
        for right in request.windows[index + 1 :]:
            if left.day_of_week != right.day_of_week:
                continue
            if left.start_time is None or right.start_time is None:
                raise ButlerError("AVAILABILITY_OVERLAP", "学习时间配置存在重复默认项", 400)
            if left.start_time < right.end_time and right.start_time < left.end_time:  # type: ignore[operator]
                raise ButlerError("AVAILABILITY_OVERLAP", "学习时间窗口不能重叠", 400)
