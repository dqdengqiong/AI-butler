"""butler-graph-v3 的意图、上下文与无副作用响应编排。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text

from ai_butler.agent.contracts import (
    ContextBundleV1,
    ContextItemV1,
    ExecutorResultV1,
    IntentDecisionV1,
)
from ai_butler.agent.evidence import estimate_tokens
from ai_butler.agent.runtime import ContextBudgetGuard
from ai_butler.domain.errors import ButlerError

from .shared import _json, _row

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class V3RunContext:
    """从服务端业务事实构建的预算内会话上下文。"""

    run: dict[str, object]
    user_input: str
    published_summaries: tuple[str, ...]
    recent_messages: tuple[str, ...]
    memories: tuple[str, ...]
    active_plan_titles: tuple[str, ...]
    attachment_count: int


class V3RunExecutorService:
    """把 v3 专属编排从兼容 v2 的领域执行器中隔离。"""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._owner, name)

    async def _route_v3_run(self, run_id: UUID) -> IntentDecisionV1:
        """在 Worker 内调用真实意图模型，并保存可恢复的结构化决策。"""

        context = await self._build_v3_context(run_id, include_memories=False)
        await self._emit_progress(context.run, "UNDERSTANDING_INTENT")
        decision = cast(
            IntentDecisionV1,
            await self._intent_router.route(
                context.user_input,
                recent_messages=context.recent_messages,
                published_summaries=context.published_summaries,
                active_plan_titles=context.active_plan_titles,
                pending_action=str(context.run.get("pending_action") or "") or None,
                attachment_count=context.attachment_count,
                run_id=run_id,
            ),
        )
        async with self.database.transaction() as connection:
            current = _row(
                await connection.execute(
                    text("SELECT status FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                )
            )
            if current is None or current["status"] != "RUNNING":
                return decision
            await connection.execute(
                text(
                    "UPDATE agent_runs SET output_data=jsonb_set(COALESCE(output_data,'{}'::jsonb),"
                    "'{intent}',CAST(:intent AS jsonb),true),updated_at=now() WHERE id=:id"
                ),
                {"id": run_id, "intent": _json(decision.model_dump(mode="json"))},
            )
        return decision

    async def _respond_v3_run(self, run_id: UUID, decision: IntentDecisionV1) -> None:
        """处理无需业务副作用的澄清、范围说明和真实通用回答。"""

        context = await self._build_v3_context(run_id)
        if decision.intent == "CLARIFY":
            async with self.database.transaction() as connection:
                run = _row(
                    await connection.execute(
                        text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                    )
                )
                if run is not None and run["status"] == "RUNNING":
                    await self._interrupt_for_clarification(
                        connection,
                        run,
                        decision.clarifying_question or "请具体说明你希望我协助什么。",
                    )
            return
        if decision.intent == "UNSUPPORTED":
            async with self.database.transaction() as connection:
                run = _row(
                    await connection.execute(
                        text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                    )
                )
                if run is not None and run["status"] == "RUNNING":
                    await self._complete_run(
                        connection,
                        run,
                        "这个请求涉及当前助理不应代替专业人士作出的高风险判断。"
                        "我可以帮助你整理问题、准备咨询清单或查找可靠公开资料。",
                    )
            return
        await self._emit_progress(context.run, "GENERATING_RESPONSE")
        await self._stream_complete_run(
            run_id,
            self._response_node.stream(
                user_input=context.user_input,
                published_summaries=context.published_summaries,
                recent_messages=context.recent_messages,
                memories=context.memories,
                run_id=run_id,
            ),
        )

    async def _execute_v3_run(self, run_id: UUID, decision: IntentDecisionV1) -> None:
        """执行需要领域能力的 v3 分支；模型建议不在网络等待期间持有行锁。"""

        if decision.intent == "MEMORY":
            context = await self._build_v3_context(run_id)
            handled = await self._memory.handle_explicit_command(
                UUID(str(context.run["user_id"])), context.user_input
            )
            if handled is None:
                await self._respond_v3_run(run_id, decision)
                return
            async with self.database.transaction() as connection:
                run = _row(
                    await connection.execute(
                        text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                    )
                )
                if run is not None and run["status"] == "RUNNING":
                    await self._complete_run(connection, run, handled.response)
            return
        if decision.intent == "TASK_FEEDBACK":
            context = await self._build_v3_context(run_id)
            if context.run.get("pending_action") in {"INPUT_RESUME", "APPROVAL_RESUME"}:
                await self._execute_domain_run(
                    run_id,
                    forced_intent="PLAN_ADJUST",
                    needs_web_override=decision.needs_web,
                    needs_private_override=decision.needs_private_knowledge,
                )
                return
            feedback = await self._feedback_adjust_node.analyze(
                user_input=context.user_input,
                has_active_plan=bool(context.active_plan_titles),
                run_id=run_id,
            )
            if feedback.action == "CLARIFY":
                await self._respond_v3_run(
                    run_id,
                    IntentDecisionV1(
                        intent="CLARIFY",
                        confidence=feedback.confidence,
                        clarifying_question=feedback.clarifying_question,
                    ),
                )
            elif feedback.action == "REPLAN":
                await self._execute_domain_run(
                    run_id,
                    forced_intent="PLAN_ADJUST",
                    needs_web_override=decision.needs_web,
                    needs_private_override=decision.needs_private_knowledge,
                )
            else:
                await self._respond_v3_run(run_id, decision)
            return
        await self._execute_domain_run(
            run_id,
            forced_intent=decision.intent,
            needs_web_override=decision.needs_web,
            needs_private_override=decision.needs_private_knowledge,
        )

    async def _approved_model_schedules(
        self, run_id: UUID
    ) -> tuple[UUID, dict[UUID, ExecutorResultV1]] | None:
        """读取服务端批准事实，并在事务外请求 Executor 生成任务候选。"""

        async with self.database.connect() as connection:
            approval = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM approval_decisions WHERE agent_run_id=:run_id "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"run_id": run_id},
                )
            )
            if approval is None or approval["action"] != "APPROVE":
                return None
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT i.plan_revision_id,r.content FROM approval_decision_items i "
                            "JOIN plan_revisions r ON r.id=i.plan_revision_id "
                            "WHERE i.approval_id=:approval AND r.user_id=:user_id "
                            "AND r.status='APPROVED' ORDER BY i.plan_revision_id"
                        ),
                        {"approval": approval["id"], "user_id": approval["user_id"]},
                    )
                )
                .mappings()
                .all()
            )
            revision_payloads: list[
                tuple[UUID, tuple[dict[str, object], ...], dict[str, object]]
            ] = []
            for row in rows:
                revision_id = UUID(str(row["plan_revision_id"]))
                template_rows = (
                    (
                        await connection.execute(
                            text(
                                "SELECT t.template_key,t.title,t.expected_minutes,t.schedule_rule,"
                                "t.schedule_rule->>'stage_key' AS stage_key "
                                "FROM plan_task_templates t JOIN plan_stages s ON s.id=t.stage_id "
                                "WHERE t.plan_revision_id=:revision "
                                "AND s.start_date<=:horizon AND s.end_date>=:today "
                                "ORDER BY t.sequence,t.template_key"
                            ),
                            {
                                "revision": revision_id,
                                "today": datetime.now(UTC).date(),
                                "horizon": datetime.now(UTC).date() + timedelta(days=6),
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
                templates = tuple(
                    {
                        "stage_key": str(template["stage_key"] or ""),
                        "template_key": str(template["template_key"]),
                        "title": str(template["title"]),
                        "expected_minutes": int(template["expected_minutes"]),
                        "priority": int((template["schedule_rule"] or {}).get("priority", 3)),
                        "frequency": (template["schedule_rule"] or {}).get("frequency", {}),
                    }
                    for template in template_rows
                )
                content = row["content"] if isinstance(row["content"], dict) else {}
                availability = content.get("availability", {})
                revision_payloads.append(
                    (
                        revision_id,
                        templates,
                        availability if isinstance(availability, dict) else {},
                    )
                )
        if not revision_payloads:
            raise ButlerError("PLAN_REVISION_CONFLICT", "批准的计划草稿不存在", 409)
        schedules: dict[UUID, ExecutorResultV1] = {}
        for revision_id, templates, availability in revision_payloads:
            schedules[revision_id] = await self._executor_node.schedule(
                revision_id=revision_id,
                templates=templates,
                availability=availability,
                current_date=datetime.now(UTC).date(),
                run_id=run_id,
            )
        return UUID(str(approval["id"])), schedules

    async def _build_v3_context(
        self, run_id: UUID, *, include_memories: bool = True
    ) -> V3RunContext:
        """从 PostgreSQL 与用户隔离的记忆 namespace 构建预算内节点上下文。"""

        async with self.database.connect() as connection:
            run = _row(
                await connection.execute(
                    text(
                        "SELECT r.*,s.thread_id FROM agent_runs r JOIN conversation_segments s "
                        "ON s.id=r.segment_id WHERE r.id=:id"
                    ),
                    {"id": run_id},
                )
            )
            if run is None:
                raise ButlerError("RUN_NOT_FOUND", "运行不存在", 404)
            message = _row(
                await connection.execute(
                    text("SELECT content FROM messages WHERE id=:id AND user_id=:user_id"),
                    {"id": run["pending_message_id"], "user_id": run["user_id"]},
                )
            ) or {"content": ""}
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT role,content FROM messages WHERE conversation_id=:conversation "
                            "AND user_id=:user_id AND id<>:current AND role IN ('USER','ASSISTANT') "
                            "AND content<>'' ORDER BY created_at DESC,id DESC LIMIT 20"
                        ),
                        {
                            "conversation": run["conversation_id"],
                            "user_id": run["user_id"],
                            "current": run["pending_message_id"],
                        },
                    )
                )
                .mappings()
                .all()
            )
            plan_titles = tuple(
                str(value)
                for value in (
                    await connection.execute(
                        text(
                            "SELECT title FROM plans WHERE user_id=:user_id AND status='ACTIVE' "
                            "ORDER BY updated_at DESC LIMIT 20"
                        ),
                        {"user_id": run["user_id"]},
                    )
                ).scalars()
            )
            attachment_count = int(
                (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM message_attachments ma JOIN messages m "
                            "ON m.id=ma.message_id WHERE m.id=:message AND m.user_id=:user_id"
                        ),
                        {"message": run["pending_message_id"], "user_id": run["user_id"]},
                    )
                ).scalar_one()
            )
            summary_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT summary_data FROM conversation_summaries "
                            "WHERE conversation_id=:conversation AND status='PUBLISHED' "
                            "ORDER BY version DESC,created_at DESC LIMIT 3"
                        ),
                        {"conversation": run["conversation_id"]},
                    )
                )
                .scalars()
                .all()
            )

        user_input = str(message.get("content") or "")
        memories: tuple[str, ...] = ()
        if include_memories:
            try:
                memories = await self._memory.search(UUID(str(run["user_id"])), user_input)
            except Exception:
                # 记忆是可降级上下文，不得阻塞普通会话；日志只带 run_id，不含用户正文。
                logger.warning("long-term memory lookup unavailable", extra={"run_id": str(run_id)})
        message_items = tuple(
            ContextItemV1(
                ref=f"message-{index}",
                text=f"{row['role']}: {row['content']}",
                trust_level="USER_CONTENT",
                estimated_tokens=estimate_tokens(str(row["content"])),
            )
            for index, row in enumerate(reversed(rows))
        )
        summaries = tuple(
            str(value.get("summary"))
            for value in reversed(summary_rows)
            if isinstance(value, dict) and value.get("summary")
        )
        bundle = ContextBundleV1(
            user_id=UUID(str(run["user_id"])),
            run_id=run_id,
            thread_id=str(run["thread_id"]),
            current_input=ContextItemV1(
                ref="current-input",
                text=user_input,
                trust_level="USER_CONTENT",
                estimated_tokens=estimate_tokens(user_input),
            ),
            business_facts=tuple(
                ContextItemV1(
                    ref=f"active-plan-{index}",
                    text=title,
                    trust_level="SYSTEM_FACT",
                    estimated_tokens=estimate_tokens(title),
                )
                for index, title in enumerate(plan_titles)
            ),
            summaries=tuple(
                ContextItemV1(
                    ref=f"published-summary-{index}",
                    text=value,
                    trust_level="SYSTEM_FACT",
                    estimated_tokens=estimate_tokens(value),
                )
                for index, value in enumerate(summaries)
            ),
            messages=message_items,
            memories=tuple(
                ContextItemV1(
                    ref=f"memory-{index}",
                    text=value,
                    trust_level="USER_CONTENT",
                    estimated_tokens=estimate_tokens(value),
                )
                for index, value in enumerate(memories)
            ),
        )
        budget = max(256, int(self.settings.context_window_tokens * 0.85) - 1024)
        compacted = ContextBudgetGuard(budget).compact(bundle)
        return V3RunContext(
            run=run,
            user_input=compacted.current_input.text,
            published_summaries=tuple(item.text for item in compacted.summaries),
            recent_messages=tuple(item.text for item in compacted.messages),
            memories=tuple(item.text for item in compacted.memories),
            active_plan_titles=plan_titles,
            attachment_count=attachment_count,
        )
