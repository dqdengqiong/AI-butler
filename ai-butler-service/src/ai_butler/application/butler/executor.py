from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from ai_butler.adapters.search import (
    SearchError,
    SearchRequest,
    SearchResult,
    SearchUnavailableError,
    minimize_public_query,
)
from ai_butler.adapters.vector import VectorStoreError
from ai_butler.agent.availability import (
    AvailabilityInterpretationV1,
    AvailabilityInterpreter,
)
from ai_butler.domain.errors import ButlerError

from .completion import CompletionService
from .context import ButlerContext
from .evidence_execution import EvidenceExecutionService
from .interrupts import InterruptionService
from .plan_execution import PlanExecutionService
from .shared import (
    PLAN_ACTION_PATTERN,
    PLAN_PATTERN,
    PRIVATE_SEARCH_PATTERN,
    SEARCH_PATTERN,
    TIME_PATTERN,
    WEB_FORCE_PATTERN,
    _row,
)


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
        self._emit_progress = interrupts._emit_progress
        self._interrupt_for_input = interrupts._interrupt_for_input
        self._interrupt_for_availability_confirmation = (
            interrupts._interrupt_for_availability_confirmation
        )
        self._interrupt_for_availability_clarification = (
            interrupts._interrupt_for_availability_clarification
        )
        self._create_plan_draft = planning._create_plan_draft
        self._regenerate_approval = planning._regenerate_approval
        self._complete_rag_run = evidence._complete_rag_run
        self._complete_run = completion._complete_run

    async def _execute_run(self, run_id: UUID) -> None:
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
                    {"id": snapshot["request_message_id"]},
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
                            "SELECT DISTINCT ma.file_id FROM message_attachments ma "
                            "JOIN messages m ON m.id=ma.message_id "
                            "WHERE m.agent_run_id=:id AND m.role='USER'"
                        ),
                        {"id": run_id},
                    )
                ).all()
            )

        raw_results: tuple[SearchResult, ...] = ()
        is_plan = bool(PLAN_PATTERN.search(content) and PLAN_ACTION_PATTERN.search(content))
        availability_candidate: AvailabilityInterpretationV1 | None = None
        confirmed_availability: AvailabilityInterpretationV1 | None = None
        revise_availability = False
        if is_plan and snapshot["pending_action"] != "APPROVAL_RESUME":
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
            elif current_input and (
                snapshot["pending_action"] == "INPUT_RESUME"
                or bool(TIME_PATTERN.search(current_input))
            ):
                availability_candidate = await self.availability_interpreter.interpret(
                    current_input
                )

        # 计划类请求先确认时间，再进行可能产生费用的检索；确认前不预取或缓存用户查询。
        can_retrieve = snapshot["pending_action"] != "APPROVAL_RESUME" and (
            not is_plan or confirmed_availability is not None
        )
        plan_ready = is_plan and confirmed_availability is not None
        needs_private = can_retrieve and (
            bool(attachment_file_ids) or bool(PRIVATE_SEARCH_PATTERN.search(content))
        )
        needs_web = can_retrieve and (
            plan_ready
            or bool(WEB_FORCE_PATTERN.search(content))
            or (bool(SEARCH_PATTERN.search(content)) and not attachment_file_ids)
        )
        query = minimize_public_query(content) or "公务员备考资料"
        if needs_private:
            await self._emit_progress(snapshot, "RETRIEVING_PRIVATE")
            try:
                raw_results += await self._retrieve_private_evidence(
                    UUID(str(snapshot["user_id"])), query, attachment_file_ids
                )
            except VectorStoreError as exc:
                raise ButlerError(
                    "PRIVATE_RETRIEVAL_UNAVAILABLE", "我的资料暂时无法检索，请稍后重试", 503, True
                ) from exc
        if needs_web:
            await self._emit_progress(snapshot, "SEARCHING_WEB")
            try:
                results = await self.search_provider.search(
                    SearchRequest(query=query, max_results=self.settings.search_max_results)
                )
            except SearchUnavailableError as exc:
                raise ButlerError(
                    "SEARCH_PROVIDER_UNAVAILABLE", "联网搜索暂时不可用，请稍后重试", 503, True
                ) from exc
            except SearchError as exc:
                raise ButlerError("SEARCH_PROVIDER_INVALID", "联网搜索返回无效结果", 502) from exc
            raw_results += results
        evidence = self.evidence_gate.normalize(raw_results, limit=self.settings.search_max_results)
        needs_search = needs_private or needs_web
        if needs_search:
            await self._emit_progress(snapshot, "ORGANIZING_CITATIONS")

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
                await self._complete_run(connection, run, response)
                return
            if is_plan:
                if confirmed_availability is not None:
                    await self._create_plan_draft(
                        connection, run, content, evidence, confirmed_availability
                    )
                elif revise_availability:
                    await self._interrupt_for_availability_clarification(
                        connection, run, "好的，请重新描述你的学习时间。"
                    )
                elif availability_candidate is not None:
                    await self._interrupt_for_availability_confirmation(
                        connection, run, availability_candidate
                    )
                else:
                    await self._interrupt_for_input(connection, run)
                return
            if needs_search:
                await self._complete_rag_run(connection, run, evidence)
                return
            await self._complete_run(
                connection,
                run,
                "我目前可以协助公务员备考规划、资料检索、任务跟进和计划调整。请告诉我目标考试与可投入时间。",
            )

    async def _retrieve_private_evidence(
        self,
        user_id: UUID,
        query: str,
        allowed_file_ids: tuple[UUID, ...],
    ) -> tuple[SearchResult, ...]:
        """向量召回后重新读取 PostgreSQL 所有权事实，拒绝信任 Qdrant payload 授权。"""

        vector = await self.embedding_provider.embed(query)
        chunk_ids = await self.vector_store.search(
            user_id, vector, self.settings.search_max_results
        )
        if not chunk_ids:
            return ()
        parameters: dict[str, object] = {"user_id": user_id, "chunk_ids": list(chunk_ids)}
        if allowed_file_ids:
            parameters["file_ids"] = list(allowed_file_ids)
            query_text = text(
                "SELECT kc.id,kc.content,kd.title,kd.stored_file_id FROM knowledge_chunks kc "
                "JOIN knowledge_documents kd ON kd.id=kc.document_id "
                "JOIN stored_files sf ON sf.id=kd.stored_file_id "
                "WHERE kc.id=ANY(:chunk_ids) AND kd.owner_user_id=:user_id "
                "AND kd.visibility='PRIVATE' AND kd.ingestion_status='READY' "
                "AND sf.upload_status='VERIFIED' AND sf.scan_status='CLEAN' "
                "AND kd.stored_file_id=ANY(:file_ids)"
            )
        else:
            query_text = text(
                "SELECT kc.id,kc.content,kd.title,kd.stored_file_id FROM knowledge_chunks kc "
                "JOIN knowledge_documents kd ON kd.id=kc.document_id "
                "JOIN stored_files sf ON sf.id=kd.stored_file_id "
                "WHERE kc.id=ANY(:chunk_ids) AND kd.owner_user_id=:user_id "
                "AND kd.visibility='PRIVATE' AND kd.ingestion_status='READY' "
                "AND sf.upload_status='VERIFIED' AND sf.scan_status='CLEAN'"
            )
        async with self.database.connect() as connection:
            rows = (await connection.execute(query_text, parameters)).mappings().all()
        by_id = {UUID(str(row["id"])): row for row in rows}
        return tuple(
            SearchResult(
                evidence_ref=f"private-{chunk_id}",
                title=str(by_id[chunk_id]["title"]),
                source_organization="我的资料",
                content=str(by_id[chunk_id]["content"]),
                score=max(0.0, 1.0 - index * 0.01),
                url=None,
                source_type="PRIVATE_FILE",
                knowledge_chunk_id=chunk_id,
            )
            for index, chunk_id in enumerate(chunk_ids)
            if chunk_id in by_id
        )
