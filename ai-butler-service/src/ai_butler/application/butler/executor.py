from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text

from ai_butler.adapters.embedding import EmbeddingProviderError
from ai_butler.adapters.search import (
    SearchError,
    SearchRequest,
    SearchResult,
    SearchUnavailableError,
    minimize_public_query,
    normalize_private_query,
)
from ai_butler.adapters.vector import VectorStoreError
from ai_butler.agent.availability import (
    AvailabilityInterpretationV1,
    AvailabilityInterpreter,
)
from ai_butler.agent.contracts import (
    ExecutorResultV1,
    IntentDecisionV1,
    PlannerResultV1,
    PlanScopeV1,
)
from ai_butler.agent.evidence import estimate_tokens
from ai_butler.agent.model_nodes import (
    ExecutorNode,
    FeedbackAdjustNode,
    IntentRouterNode,
    ResponseNode,
)
from ai_butler.agent.planning_nodes import DeterministicPlanReview, PlannerNode
from ai_butler.domain.errors import ButlerError

from .completion import CompletionService
from .context import ButlerContext
from .evidence_execution import EvidenceExecutionService
from .interrupts import InterruptionService
from .memory import LongTermMemoryService
from .plan_execution import PlanExecutionService
from .plan_scope_flow import PlanScopeFlowService
from .private_retrieval import PrivateEvidenceRetriever
from .shared import (
    PLAN_ACTION_PATTERN,
    PLAN_PATTERN,
    PRIVATE_SEARCH_PATTERN,
    SEARCH_PATTERN,
    TIME_PATTERN,
    WEB_FORCE_PATTERN,
    _row,
)
from .v3_executor import V3RunExecutorService

logger = logging.getLogger(__name__)


class RunExecutor:
    def __init__(
        self,
        context: ButlerContext,
        interrupts: InterruptionService,
        planning: PlanExecutionService,
        evidence: EvidenceExecutionService,
        completion: CompletionService,
    ) -> None:
        self.database = context.database
        self.settings = context.settings
        self.search_provider = context.search_provider
        self.embedding_provider = context.embedding_provider
        self.vector_store = context.vector_store
        self.availability_interpreter = context.availability_interpreter
        self.evidence_gate = context.evidence_gate
        self.research_input_tokens = context.research_input_tokens
        self._emit_progress = interrupts._emit_progress
        self._interrupt_for_input = interrupts._interrupt_for_input
        self._interrupt_for_availability_confirmation = (
            interrupts._interrupt_for_availability_confirmation
        )
        self._interrupt_for_availability_clarification = (
            interrupts._interrupt_for_availability_clarification
        )
        self._interrupt_for_clarification = interrupts._interrupt_for_clarification
        self._create_model_plan_draft = planning._create_model_plan_draft
        self._regenerate_approval = planning._regenerate_approval
        self._materialize_approval = planning._materialize_approval
        self._materialize_model_approval = planning._materialize_model_approval
        self._complete_rag_run = evidence._complete_rag_run
        self._generate_rag_answer = evidence._generate_rag_answer
        self._complete_run = completion._complete_run
        self._stream_complete_run = completion._stream_complete_run
        self._memory = LongTermMemoryService(context)
        self._intent_router = IntentRouterNode(context.llm)
        self._planner_node = PlannerNode(context.llm)
        self._plan_review = DeterministicPlanReview()
        self._executor_node = ExecutorNode(context.llm)
        self._feedback_adjust_node = FeedbackAdjustNode(context.llm)
        self._response_node = ResponseNode(context.llm)
        self._v3 = V3RunExecutorService(self)
        self._private_retriever = PrivateEvidenceRetriever(context)
        self._plan_scope_flow = PlanScopeFlowService(context, interrupts)

    async def _route_v3_run(self, run_id: UUID) -> IntentDecisionV1:
        return await self._v3._route_v3_run(run_id)

    async def _respond_v3_run(self, run_id: UUID, decision: IntentDecisionV1) -> None:
        await self._v3._respond_v3_run(run_id, decision)

    async def _execute_v3_run(self, run_id: UUID, decision: IntentDecisionV1) -> None:
        await self._v3._execute_v3_run(run_id, decision)

    async def _approved_model_schedules(
        self, run_id: UUID
    ) -> tuple[UUID, dict[UUID, ExecutorResultV1]] | None:
        return await self._v3._approved_model_schedules(run_id)

    async def _retrieve_private_evidence(
        self,
        user_id: UUID,
        query: str,
        allowed_file_ids: tuple[UUID, ...],
    ) -> tuple[SearchResult, ...]:
        return await self._private_retriever._retrieve_private_evidence(
            user_id, query, allowed_file_ids
        )

    async def _execute_legacy_run(self, run_id: UUID) -> None:
        """保持 v2 checkpoint 的正则分支语义，直到旧 run 全部进入终态。"""

        await self._execute_domain_run(run_id)

    async def _execute_domain_run(
        self,
        run_id: UUID,
        *,
        forced_intent: str | None = None,
        needs_web_override: bool | None = None,
        needs_private_override: bool | None = None,
    ) -> None:
        """在事务外完成联网检索，再用短事务原子写入回答与引用。

        run 领取和最终持久化分别加锁；搜索期间用户可能请求取消，因此写入前
        必须重新检查 ``RUNNING``。外部查询使用脱敏后的最小文本，不携带身份。
        """

        async with self.database.connect() as connection:
            snapshot = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id"), {"id": run_id}
                )
            )
            if snapshot is None or snapshot["status"] != "RUNNING":
                return
            request_message = _row(
                await connection.execute(
                    text("SELECT content,structured_content FROM messages WHERE id=:id"),
                    {"id": snapshot["pending_message_id"]},
                )
            ) or {"content": "", "structured_content": {}}
            current_input = str(request_message.get("content") or "")
            request_context = request_message.get("structured_content")
            request_context = request_context if isinstance(request_context, dict) else {}
            content = str(
                (
                    await connection.execute(
                        text(
                            "SELECT string_agg(content,' ' ORDER BY created_at,id) FROM messages "
                            "WHERE agent_run_id=:id AND role='USER'"
                        ),
                        {"id": run_id},
                    )
                ).scalar_one_or_none()
                or ""
            )
            attachment_file_ids = tuple(
                UUID(str(row[0]))
                for row in (
                    await connection.execute(
                        text(
                            "SELECT DISTINCT ma.stored_file_id FROM message_attachments ma "
                            "JOIN messages m ON m.id=ma.message_id "
                            "WHERE m.agent_run_id=:id AND m.role='USER'"
                        ),
                        {"id": run_id},
                    )
                ).all()
            )

        memory_command = await self._memory.handle_explicit_command(
            UUID(str(snapshot["user_id"])), current_input
        )
        if memory_command is not None:
            async with self.database.transaction() as connection:
                run = _row(
                    await connection.execute(
                        text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                    )
                )
                if run is not None and run["status"] == "RUNNING":
                    await self._complete_run(connection, run, memory_command.response)
            return

        model_schedules: tuple[UUID, dict[UUID, ExecutorResultV1]] | None = None
        if forced_intent is not None and snapshot["pending_action"] == "APPROVAL_RESUME":
            await self._emit_progress(snapshot, "SCHEDULING_TASKS")
            model_schedules = await self._approved_model_schedules(run_id)

        raw_results: tuple[SearchResult, ...] = ()
        is_plan = (
            forced_intent in {"PLAN_CREATE", "PLAN_ADJUST"}
            if forced_intent is not None
            else bool(PLAN_PATTERN.search(content) and PLAN_ACTION_PATTERN.search(content))
        )
        availability_candidate: AvailabilityInterpretationV1 | None = None
        confirmed_availability: AvailabilityInterpretationV1 | None = None
        plan_scope: PlanScopeV1 | None = None
        revise_availability = False
        if is_plan and snapshot["pending_action"] != "APPROVAL_RESUME":
            output_state = snapshot.get("output_data")
            output_state = output_state if isinstance(output_state, dict) else {}
            has_plan_scope_state = bool(
                output_state.get("plan_scope_draft") or output_state.get("plan_scope")
            )
            phase = str(request_context.get("card_phase") or "")
            selected_options = request_context.get("selected_options")
            selected_option = (
                selected_options[0]
                if isinstance(selected_options, list)
                and selected_options
                and isinstance(selected_options[0], dict)
                else {}
            )
            selected_option_id = str(selected_option.get("id") or "")
            if phase == "CONFIRM_AVAILABILITY" and selected_option_id == "confirm-availability":
                try:
                    validated_confirmation = AvailabilityInterpreter.normalize(
                        AvailabilityInterpretationV1.model_validate(
                            request_context.get("interpretation")
                        )
                    )
                    if validated_confirmation.status == "COMPLETE":
                        confirmed_availability = validated_confirmation
                    else:
                        availability_candidate = validated_confirmation
                except ValueError:
                    availability_candidate = AvailabilityInterpretationV1(
                        status="NEEDS_CLARIFICATION",
                        question="时间确认信息已经失效，请重新描述你的学习时间。",
                    )
            elif phase == "CONFIRM_AVAILABILITY" and selected_option_id == "revise-availability":
                revise_availability = True
            elif isinstance(selected_option.get("availability"), dict):
                try:
                    availability_candidate = AvailabilityInterpreter.normalize(
                        AvailabilityInterpretationV1.model_validate(selected_option["availability"])
                    )
                except ValueError:
                    availability_candidate = AvailabilityInterpretationV1(
                        status="NEEDS_CLARIFICATION",
                        question="这个快捷选项暂时不可用，请直接描述你的学习时间。",
                    )
            elif (
                not has_plan_scope_state
                and current_input
                and (
                    snapshot["pending_action"] == "INPUT_RESUME"
                    or bool(TIME_PATTERN.search(current_input))
                )
            ):
                availability_candidate = await self.availability_interpreter.interpret(
                    current_input,
                    run_id=run_id,
                )

            plan_scope = await self._plan_scope_flow.resolve(
                run_id=run_id,
                snapshot=snapshot,
                objective=content,
                current_input=current_input,
                request_context=request_context,
                availability_candidate=availability_candidate,
                confirmed_availability=confirmed_availability,
                revise_availability=revise_availability,
            )
            if plan_scope is None:
                return
            confirmed_availability = plan_scope.availability

        # 计划类请求先确认时间，再进行可能产生费用的检索；确认前不预取或缓存用户查询。
        can_retrieve = snapshot["pending_action"] != "APPROVAL_RESUME" and (
            not is_plan or plan_scope is not None
        )
        plan_ready = is_plan and plan_scope is not None
        inferred_private = bool(attachment_file_ids) or bool(PRIVATE_SEARCH_PATTERN.search(content))
        needs_private = can_retrieve and (
            needs_private_override if needs_private_override is not None else inferred_private
        )
        inferred_web = (
            plan_ready
            or bool(WEB_FORCE_PATTERN.search(content))
            or (bool(SEARCH_PATTERN.search(content)) and not attachment_file_ids)
        )
        needs_web = can_retrieve and (
            needs_web_override if needs_web_override is not None else inferred_web
        )
        private_query = normalize_private_query(content) or "公务员备考资料"
        public_query = minimize_public_query(content) or "公务员备考资料"
        if needs_private:
            await self._emit_progress(snapshot, "RESEARCHING")
            await self._emit_progress(snapshot, "RETRIEVING_PRIVATE")
            try:
                raw_results += await self._retrieve_private_evidence(
                    UUID(str(snapshot["user_id"])), private_query, attachment_file_ids
                )
            except (EmbeddingProviderError, VectorStoreError) as exc:
                raise ButlerError(
                    "PRIVATE_RETRIEVAL_UNAVAILABLE", "我的资料暂时无法检索，请稍后重试", 503, True
                ) from exc
        if needs_web:
            await self._emit_progress(snapshot, "RESEARCHING")
            await self._emit_progress(snapshot, "SEARCHING_WEB")
            try:
                results = await self.search_provider.search(
                    SearchRequest(
                        query=public_query,
                        max_results=self.settings.search_candidate_results,
                    )
                )
            except SearchUnavailableError as exc:
                raise ButlerError(
                    "SEARCH_PROVIDER_UNAVAILABLE", "联网搜索暂时不可用，请稍后重试", 503, True
                ) from exc
            except SearchError as exc:
                raise ButlerError("SEARCH_PROVIDER_INVALID", "联网搜索返回无效结果", 502) from exc
            raw_results += results
        evidence_budget = min(
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
            token_budget=evidence_budget,
            max_item_tokens=self.settings.rag_evidence_max_item_tokens,
        )
        needs_search = needs_private or needs_web
        if needs_search:
            await self._emit_progress(snapshot, "ORGANIZING_CITATIONS")
        rag_answer = None
        if needs_search and not is_plan and evidence:
            await self._emit_progress(snapshot, "GENERATING_ANSWER")
            rag_answer = await self._generate_rag_answer(content, evidence, run_id=run_id)

        planner_result: PlannerResultV1 | None = None
        if plan_ready and confirmed_availability is not None and plan_scope is not None:
            await self._emit_progress(snapshot, "BUILDING_PLAN")
            async with self.database.connect() as connection:
                existing = _row(
                    await connection.execute(
                        text(
                            "SELECT title,status,current_revision_id FROM plans "
                            "WHERE user_id=:user_id AND status='ACTIVE' "
                            "ORDER BY updated_at DESC LIMIT 1"
                        ),
                        {"user_id": snapshot["user_id"]},
                    )
                )
            existing_plan = (
                {
                    "title": str(existing["title"]),
                    "status": str(existing["status"]),
                    "has_published_revision": existing["current_revision_id"] is not None,
                }
                if existing is not None and forced_intent == "PLAN_ADJUST"
                else None
            )
            verified_claims: tuple[dict[str, object], ...] = tuple(
                {
                    "claim_key": item.result.evidence_ref,
                    "text": item.result.content,
                    "source_level": item.source_level,
                }
                for item in evidence
            )
            available_weekly = int(confirmed_availability.weekly_minutes or 0)
            plan_intent = forced_intent or "PLAN_CREATE"
            if plan_intent == "PLAN_ADJUST" and existing is None:
                planner_result = PlannerResultV1(
                    status="NEEDS_INPUT",
                    question="当前没有生效计划可调整。你希望创建一个新计划吗？",
                )
            else:
                planner_result = await self._planner_node.plan(
                    objective=content,
                    weekly_minutes=max(1, int(available_weekly * 0.85)),
                    availability=confirmed_availability.model_dump(mode="json"),
                    verified_claims=verified_claims,
                    plan_scope=plan_scope,
                    existing_plan=existing_plan,
                    run_id=run_id,
                )
            await self._emit_progress(snapshot, "REVIEWING_PLAN")
            self._plan_review.validate(
                planner_result,
                available_weekly_minutes=available_weekly,
                allowed_claim_keys=(item.result.evidence_ref for item in evidence),
                expected_start_date=plan_scope.start_date,
                expected_end_date=plan_scope.target_date,
            )

        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                )
            )
            if run is None or run["status"] != "RUNNING":
                return
            # 普通管家会话只有在识别到明确的考公意图后才绑定专业 Agent。
            # 专业会话在创建 run 时已经固定选择，worker 不得覆盖或改路由。
            if run["selected_user_agent_id"] is None and PLAN_PATTERN.search(content):
                selected_agent_id = (
                    await connection.execute(
                        text(
                            "SELECT ua.id FROM user_agents ua "
                            "JOIN agent_definitions ad ON ad.id=ua.agent_definition_id "
                            "WHERE ua.user_id=:user_id AND ua.status='ACTIVE' "
                            "AND ad.code='CIVIL_SERVICE_EXAM' "
                            "AND ad.status='ACTIVE' AND ad.catalog_status='AVAILABLE' LIMIT 1"
                        ),
                        {"user_id": run["user_id"]},
                    )
                ).scalar_one_or_none()
                if selected_agent_id is not None:
                    await connection.execute(
                        text(
                            "UPDATE agent_runs SET selected_user_agent_id=:selected_agent_id "
                            "WHERE id=:run_id"
                        ),
                        {"selected_agent_id": selected_agent_id, "run_id": run_id},
                    )
                    run["selected_user_agent_id"] = selected_agent_id
            if run["pending_action"] == "APPROVAL_RESUME":
                approval = _row(
                    await connection.execute(
                        text(
                            "SELECT * FROM approval_decisions WHERE agent_run_id=:id ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"id": run_id},
                    )
                )
                action = approval["action"] if approval else "REJECT"
                response = {
                    "APPROVE": "计划已确认，正式任务已经生成。你可以在首页开始今天的学习。",
                    "REJECT": "好的，这份草案不会生效，也没有创建任何正式任务。",
                    "EDIT": "收到修改意见。我会在下一版草案中降低负荷并保留必要复习。",
                }.get(action, "审批已处理。")
                if action == "EDIT" and approval:
                    await self._regenerate_approval(connection, run, approval)
                    return
                if action == "APPROVE" and approval:
                    if forced_intent is not None:
                        if model_schedules is None:
                            raise ButlerError("EXECUTOR_MODEL_INVALID", "批准后的任务排期缺失", 502)
                        model_approval_id, schedules = model_schedules
                        if model_approval_id != UUID(str(approval["id"])):
                            raise ButlerError(
                                "APPROVAL_VERSION_CONFLICT", "审批版本已更新，请重试", 409
                            )
                        await self._materialize_model_approval(
                            connection,
                            UUID(str(run["user_id"])),
                            model_approval_id,
                            schedules,
                        )
                    else:
                        await self._materialize_approval(
                            connection, UUID(str(run["user_id"])), UUID(str(approval["id"]))
                        )
                await self._complete_run(connection, run, response)
                return
            if is_plan:
                if confirmed_availability is not None and plan_scope is not None:
                    if planner_result is None:
                        raise ButlerError("PLANNER_MODEL_INVALID", "计划草稿缺失", 502)
                    if planner_result.status == "NEEDS_INPUT":
                        await self._interrupt_for_clarification(
                            connection,
                            run,
                            planner_result.question or "请补充计划目标。",
                        )
                    elif planner_result.status == "INFEASIBLE":
                        options = "；".join(planner_result.adjustment_options)
                        response = "当前条件下无法生成可执行计划。"
                        if options:
                            response += f"可以考虑：{options}。"
                        await self._complete_run(connection, run, response)
                    else:
                        await self._create_model_plan_draft(
                            connection,
                            run,
                            content,
                            evidence,
                            confirmed_availability,
                            plan_scope,
                            planner_result,
                            intent=forced_intent or "PLAN_CREATE",
                        )
                else:
                    raise ButlerError("PLAN_SCOPE_MISSING", "计划范围尚未确认", 409)
                return
            if needs_search:
                await self._complete_rag_run(connection, run, evidence, rag_answer)
                return
            await self._complete_run(
                connection,
                run,
                "我目前可以协助公务员备考规划、资料检索、任务跟进和计划调整。请告诉我目标考试与可投入时间。",
            )
