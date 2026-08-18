from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from ai_butler.adapters.embedding import EmbeddingProviderError
from ai_butler.adapters.search import (
    SearchError,
    SearchResult,
    SearchUnavailableError,
    minimize_public_query,
    normalize_private_query,
)
from ai_butler.adapters.vector import VectorStoreError
from ai_butler.agent.availability import AvailabilityInterpreter
from ai_butler.agent.contracts import IntentDecisionV1
from ai_butler.agent.evidence import estimate_tokens
from ai_butler.agent.model_nodes import ExecutorNode, IntentRouterNode, ResponseNode
from ai_butler.agent.planning_nodes import DeterministicPlanReview, PlannerNode
from ai_butler.domain.errors import ButlerError
from ai_butler.tools import (
    DEFAULT_TOOL_REGISTRY,
    PlanRequirementCollector,
    search_private_knowledge,
    search_public_knowledge,
)

from .completion import CompletionService
from .context import ButlerContext
from .events import EventService
from .evidence_execution import EvidenceExecutionService
from .memory import LongTermMemoryService
from .private_retrieval import PrivateEvidenceRetriever
from .run_execution import RunExecutionService
from .shared import _row


class RunExecutor:
    """执行代码白名单能力；模型不会获得工具、权限或实体标识。"""

    def __init__(
        self,
        context: ButlerContext,
        events: EventService,
        evidence: EvidenceExecutionService,
        completion: CompletionService,
    ) -> None:
        self.database = context.database
        self.settings = context.settings
        self.search_provider = context.search_provider
        self.evidence_gate = context.evidence_gate
        self.research_input_tokens = context.research_input_tokens
        self._append_event = events._append_event
        self._complete_rag_run = evidence._complete_rag_run
        self._generate_rag_answer = evidence._generate_rag_answer
        self._complete_run = completion._complete_run
        self._complete_validated_run = completion._complete_validated_run
        self._stream_complete_run = completion._stream_complete_run
        self._memory = LongTermMemoryService(context)
        self._intent_router = IntentRouterNode(context.llm)
        self._planner_node = PlannerNode(context.llm)
        self._plan_review = DeterministicPlanReview()
        self._executor_node = ExecutorNode(context.llm)
        self._plan_requirements = PlanRequirementCollector(AvailabilityInterpreter(context.llm))
        self._response_node = ResponseNode(context.llm)
        self._private_retriever = PrivateEvidenceRetriever(context)
        self._execution = RunExecutionService(self)

    async def _route_run(self, run_id: UUID) -> IntentDecisionV1:
        return await self._execution.route(run_id)

    async def _respond_run(self, run_id: UUID, decision: IntentDecisionV1) -> None:
        await self._execution.respond(run_id, decision)

    async def _execute_run(self, run_id: UUID, decision: IntentDecisionV1) -> None:
        await self._execution.execute(run_id, decision)

    async def _emit_progress(self, run: dict[str, Any], code: str) -> None:
        async with self.database.transaction() as connection:
            current = _row(
                await connection.execute(
                    text("SELECT status FROM agent_runs WHERE id=:id FOR UPDATE"),
                    {"id": run["id"]},
                )
            )
            if current is None or current["status"] != "RUNNING":
                return
            await self._append_event(
                connection,
                run["id"],
                run["user_id"],
                "progress",
                {"code": code},
                run["attempt"],
            )

    async def _retrieve_private_evidence(
        self, user_id: UUID, query: str, allowed_file_ids: tuple[UUID, ...]
    ) -> tuple[SearchResult, ...]:
        return await self._private_retriever._retrieve_private_evidence(
            user_id, query, allowed_file_ids
        )

    async def _execute_domain_run(
        self,
        run_id: UUID,
        *,
        public_search_enabled: bool = False,
        private_search_enabled: bool = False,
    ) -> None:
        async with self.database.connect() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id"), {"id": run_id}
                )
            )
            if run is None or run["status"] != "RUNNING":
                return
            message = _row(
                await connection.execute(
                    text("SELECT content FROM messages WHERE id=:id"),
                    {"id": run["pending_message_id"]},
                )
            ) or {"content": ""}
            content = str(message["content"] or "")
            attachment_ids = tuple(
                UUID(str(value))
                for value in (
                    await connection.execute(
                        text(
                            "SELECT stored_file_id FROM message_attachments WHERE message_id=:id "
                            "ORDER BY position"
                        ),
                        {"id": run["pending_message_id"]},
                    )
                ).scalars()
            )
        needs_private = private_search_enabled or bool(attachment_ids)
        raw_results: tuple[SearchResult, ...] = ()
        if needs_private:
            DEFAULT_TOOL_REGISTRY.require("search_private_knowledge", "ToolExecutor")
            await self._emit_progress(run, "RETRIEVING_PRIVATE")
            try:
                raw_results += await search_private_knowledge(
                    self._retrieve_private_evidence,
                    UUID(str(run["user_id"])),
                    normalize_private_query(content) or "用户资料",
                    attachment_ids,
                )
            except (EmbeddingProviderError, VectorStoreError) as exc:
                raise ButlerError(
                    "PRIVATE_RETRIEVAL_UNAVAILABLE", "我的资料暂时无法检索", 503, True
                ) from exc
        if public_search_enabled:
            DEFAULT_TOOL_REGISTRY.require("search_public_knowledge", "ToolExecutor")
            await self._emit_progress(run, "SEARCHING_WEB")
            try:
                raw_results += await search_public_knowledge(
                    self.search_provider,
                    minimize_public_query(content) or "公务员考试资料",
                    self.settings.search_candidate_results,
                )
            except SearchUnavailableError as exc:
                raise ButlerError(
                    "SEARCH_PROVIDER_UNAVAILABLE", "联网搜索暂时不可用", 503, True
                ) from exc
            except SearchError as exc:
                raise ButlerError("SEARCH_PROVIDER_INVALID", "联网搜索返回无效结果", 502) from exc
        budget = min(
            self.settings.rag_evidence_max_tokens,
            max(
                0,
                self.research_input_tokens
                - estimate_tokens(content)
                - self.settings.rag_token_safety_margin,
            ),
        )
        evidence = self.evidence_gate.normalize(
            raw_results,
            limit=self.settings.search_max_results,
            token_budget=budget,
            max_item_tokens=self.settings.rag_evidence_max_item_tokens,
        )
        answer = None
        if evidence:
            await self._emit_progress(run, "GENERATING_ANSWER")
            answer = await self._generate_rag_answer(content, evidence, run_id=run_id)
        async with self.database.transaction() as connection:
            current = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                )
            )
            if current is None or current["status"] != "RUNNING":
                return
            await self._complete_rag_run(connection, current, evidence, answer)
