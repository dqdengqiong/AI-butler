"""单轮意图路由、白名单能力执行与只读计划预览。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from ai_butler.adapters.search import SearchError, SearchUnavailableError
from ai_butler.agent.availability import expand_availability_calendar
from ai_butler.agent.contracts import IntentDecisionV1, PlanScopeV1
from ai_butler.api.schemas import PlanPreviewCardPayloadV1
from ai_butler.domain.errors import ButlerError
from ai_butler.tools import (
    DEFAULT_TOOL_REGISTRY,
    PlanRequirementsV1,
    handle_memory_command,
    prepare_plan_preview,
    read_plan_context,
    read_task_context,
    schedule_plan_window,
    search_public_knowledge,
)

from ..shared import _content_hash, _json, _row
from .context import RunContext, build_run_context
from .workflow import WorkflowSessionMixin


class RunExecutionService(WorkflowSessionMixin):
    """模型只分类和生成内容；代码决定能力、读取范围与业务写权限。"""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._owner, name)

    async def route(self, run_id: UUID) -> IntentDecisionV1:
        context = await self._build_context(run_id, include_memories=False, task="ROUTER")
        await self._emit_progress(context.run, "UNDERSTANDING_INTENT")
        decision = cast(
            IntentDecisionV1,
            await self._intent_router.route(
                context.user_input,
                recent_messages=context.recent_messages,
                published_summaries=context.published_summaries,
                active_plan_titles=context.active_plan_titles,
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
            if current is not None and current["status"] == "RUNNING":
                await connection.execute(
                    text(
                        "UPDATE agent_runs SET output_data=jsonb_set(COALESCE(output_data,'{}'::jsonb),"
                        "'{intent}',CAST(:intent AS jsonb),true),updated_at=now() WHERE id=:id"
                    ),
                    {"id": run_id, "intent": _json(decision.model_dump(mode="json"))},
                )
        return decision

    async def respond(self, run_id: UUID, decision: IntentDecisionV1) -> None:
        context = await self._build_context(run_id, task="GENERAL")
        if decision.intent == "CLARIFY":
            await self._complete_text(
                run_id, decision.clarifying_question or "请具体说明你希望我协助什么。"
            )
            return
        if decision.intent == "UNSUPPORTED":
            await self._complete_text(
                run_id,
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

    async def execute(self, run_id: UUID, decision: IntentDecisionV1) -> None:
        task: Literal["GENERAL", "PLANNING", "RESEARCH"] = (
            "PLANNING"
            if decision.intent in {"PLAN_CREATE", "PLAN_ADJUST", "DAILY_PLANNING", "PLAN_REVIEW"}
            else "RESEARCH"
            if decision.intent in {"RESEARCH", "CIVIL_QA"}
            else "GENERAL"
        )
        context = await self._build_context(run_id, task=task)
        tool_plan = DEFAULT_TOOL_REGISTRY.resolve(decision.intent, decision.context_needs)
        if decision.intent in {"PLAN_CREATE", "PLAN_ADJUST"}:
            await self._collect_and_prepare_plan(run_id, context, decision.intent)
            return
        if decision.intent == "MEMORY":
            handled = await handle_memory_command(
                self._memory.handle_explicit_command,
                UUID(str(context.run["user_id"])),
                context.user_input,
            )
            if handled is None:
                await self.respond(run_id, decision)
            else:
                await self._complete_text(run_id, handled.response)
            return
        if "read_plan_context" in tool_plan or "read_task_context" in tool_plan:
            facts = await self._load_planning_facts(context, decision)
            answer = await self._response_node.generate_verified(verified_data=facts, run_id=run_id)
            await self._complete_text(run_id, answer, validated=True)
            return
        await self._execute_domain_run(
            run_id,
            public_search_enabled="search_public_knowledge" in tool_plan,
            private_search_enabled="search_private_knowledge" in tool_plan,
        )

    async def _collect_and_prepare_plan(
        self, run_id: UUID, context: RunContext, intent: str
    ) -> None:
        DEFAULT_TOOL_REGISTRY.require("collect_plan_requirements", "ToolExecutor", intent)
        async with self.database.connect() as connection:
            timezone_name = str(
                (
                    await connection.execute(
                        text("SELECT timezone FROM users WHERE id=:id"),
                        {"id": context.run["user_id"]},
                    )
                ).scalar_one()
            )
        today = datetime.now(ZoneInfo(timezone_name)).date()
        workflow = await self._active_plan_workflow(context)
        result = await self._plan_requirements.collect(
            current_input=context.user_input,
            recent_messages=context.recent_messages,
            start_date=today,
            run_id=run_id,
            existing_slots=(workflow.get("slots") if workflow is not None else None),
        )
        if result.status == "NEEDS_CLARIFICATION":
            await self._save_plan_workflow(context, workflow, result.data or {})
            await self._complete_text(run_id, result.clarification or "请补充计划信息。")
            return
        await self._complete_plan_workflow(workflow)
        requirements = cast(PlanRequirementsV1, result.data)
        baseline = await self._resolve_adjustment_baseline(context, intent)
        if intent == "PLAN_ADJUST" and baseline is None:
            return
        await self._execute_plan_preview(run_id, context, requirements, intent, baseline)

    async def _resolve_adjustment_baseline(
        self, context: RunContext, intent: str
    ) -> dict[str, object] | None:
        if intent != "PLAN_ADJUST":
            return None
        async with self.database.connect() as connection:
            plans = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT id,title,status,current_revision_id FROM plans "
                            "WHERE user_id=:user_id AND status='ACTIVE' "
                            "ORDER BY updated_at DESC,id"
                        ),
                        {"user_id": context.run["user_id"]},
                    )
                ).mappings()
            ]
        if not plans:
            await self._complete_text(UUID(str(context.run["id"])), "目前没有可调整的进行中计划。")
            return None
        mentioned = [item for item in plans if str(item["title"]) in context.user_input]
        if len(plans) > 1 and len(mentioned) != 1:
            titles = "、".join(str(item["title"]) for item in plans)
            await self._complete_text(
                UUID(str(context.run["id"])), f"你想调整哪一个计划？当前有：{titles}。"
            )
            return None
        return mentioned[0] if mentioned else plans[0]

    async def _execute_plan_preview(
        self,
        run_id: UUID,
        context: RunContext,
        requirements: PlanRequirementsV1,
        intent: str,
        baseline: dict[str, object] | None,
    ) -> None:
        DEFAULT_TOOL_REGISTRY.require("prepare_plan_preview", "ToolExecutor", intent)
        DEFAULT_TOOL_REGISTRY.require("search_public_knowledge", "ToolExecutor", intent)
        if requirements.target_date <= requirements.start_date:
            raise ButlerError("PLAN_TARGET_DATE_INVALID", "目标日期必须晚于今天", 422)
        availability = requirements.availability
        weekly_minutes = int(availability.weekly_minutes or 0)
        scope = PlanScopeV1(
            objective_summary=requirements.objective_summary,
            availability=availability,
            start_date=requirements.start_date,
            target_date=requirements.target_date,
            period_source=requirements.period_source,
        )
        await self._emit_progress(context.run, "SEARCHING_WEB")
        try:
            results = await search_public_knowledge(
                self.search_provider,
                requirements.objective_summary,
                self.settings.search_candidate_results,
            )
        except SearchUnavailableError as exc:
            raise ButlerError(
                "SEARCH_PROVIDER_UNAVAILABLE", "联网搜索暂时不可用", 503, True
            ) from exc
        except SearchError as exc:
            raise ButlerError("SEARCH_PROVIDER_INVALID", "联网搜索返回无效结果", 502) from exc
        evidence = self.evidence_gate.normalize(
            results,
            limit=self.settings.search_max_results,
            token_budget=self.settings.rag_evidence_max_tokens,
            max_item_tokens=self.settings.rag_evidence_max_item_tokens,
        )
        verified_claims = tuple(
            {
                "claim_key": item.result.evidence_ref,
                "text": item.result.content,
                "source_level": item.source_level,
            }
            for item in evidence
        )
        await self._emit_progress(context.run, "BUILDING_PLAN")
        planner = await prepare_plan_preview(
            self._planner_node.plan,
            objective=requirements.objective_summary,
            weekly_minutes=max(1, int(weekly_minutes * 0.85)),
            availability=availability.model_dump(mode="json"),
            verified_claims=verified_claims,
            plan_scope=scope,
            existing_plan=(
                {
                    "title": str(baseline["title"]),
                    "status": str(baseline["status"]),
                    "has_published_revision": True,
                }
                if baseline
                else None
            ),
            run_id=run_id,
        )
        if planner.status != "READY" or planner.plan is None:
            message = planner.question or "当前条件无法生成可执行计划。"
            if planner.adjustment_options:
                message += " 可以考虑：" + "；".join(planner.adjustment_options)
            await self._complete_text(run_id, message)
            return
        await self._emit_progress(context.run, "REVIEWING_PLAN")
        self._plan_review.validate(
            planner,
            available_weekly_minutes=weekly_minutes,
            allowed_claim_keys=(item.result.evidence_ref for item in evidence),
            expected_start_date=requirements.start_date,
            expected_end_date=requirements.target_date,
        )
        templates = tuple(
            {
                "stage_key": stage.stage_key,
                "template_key": template.template_key,
                "title": template.title,
                "expected_minutes": template.expected_minutes,
                "priority": template.priority,
                "frequency": template.frequency,
            }
            for stage in planner.plan.stages
            for template in stage.task_templates
        )
        stages = tuple(
            {
                "stage_key": stage.stage_key,
                "start_date": stage.start_date,
                "end_date": stage.end_date,
            }
            for stage in planner.plan.stages
        )
        await self._emit_progress(context.run, "SCHEDULING_TASKS")
        DEFAULT_TOOL_REGISTRY.require("schedule_plan_window", "ToolExecutor", intent)
        preview_window_end = min(
            requirements.target_date, requirements.start_date + timedelta(days=6)
        )
        daily_availability = expand_availability_calendar(
            availability,
            start_date=requirements.start_date,
            end_date=preview_window_end,
        )
        scheduled, unscheduled = schedule_plan_window(
            revision_ref=str(run_id),
            templates=templates,
            stages=stages,
            availability=availability.model_dump(mode="json"),
            window_start=requirements.start_date,
            window_end=preview_window_end,
        )
        plan_data = planner.plan.model_dump(mode="json")
        plan_data["tasks"] = [task.model_dump(mode="json") for task in scheduled]
        evidence_data = [
            {
                "evidence_ref": item.result.evidence_ref,
                "title": item.result.title,
                "source_type": item.result.source_type,
                "source_url": item.canonical_url,
                "domain": item.domain,
                "published_at": item.result.published_at.isoformat()
                if item.result.published_at
                else None,
                "excerpt": item.result.content[:1000],
                "source_level": item.source_level,
            }
            for item in evidence
        ]
        generated_at = datetime.now(UTC)
        unsigned = {
            "operation": "ADJUST" if intent == "PLAN_ADJUST" else "CREATE",
            "title": planner.plan.title,
            "plan": plan_data,
            "total_weekly_minutes": planner.plan.weekly_minutes,
            "available_weekly_minutes": weekly_minutes,
            "generated_at": generated_at.isoformat(),
            "expires_at": (generated_at + timedelta(hours=24)).isoformat(),
            "target_plan_id": str(baseline["id"]) if baseline else None,
            "expected_current_revision_id": (
                str(baseline["current_revision_id"]) if baseline else None
            ),
            "evidence": evidence_data,
            "availability": availability.model_dump(mode="json"),
            "daily_availability": [item.model_dump(mode="json") for item in daily_availability],
            "scenario_code": requirements.scenario_code,
            "scenario_fields": requirements.scenario_fields,
            "warnings": list(planner.warnings)
            + (["部分任务因容量限制未进入七日窗口"] if unscheduled else []),
        }
        normalized_payload = PlanPreviewCardPayloadV1.model_validate(
            {"status": "READY", "preview_hash": "0" * 64, **unsigned}
        ).model_dump(mode="json")
        normalized_unsigned = {
            key: value
            for key, value in normalized_payload.items()
            if key not in {"status", "preview_hash"}
        }
        preview_hash = _content_hash(normalized_unsigned)
        payload = PlanPreviewCardPayloadV1.model_validate(
            {**normalized_payload, "preview_hash": preview_hash}
        )
        card = cast(
            dict[str, object],
            {
                "schema_version": "1.0",
                "card_id": str(uuid4()),
                "card_type": "PlanPreviewCard",
                "entity_refs": {},
                "payload": payload.model_dump(mode="json"),
                "actions": [
                    {
                        "action_id": "confirm-preview",
                        "action_type": "CONFIRM_PLAN",
                        "label": "确认计划",
                    },
                    {"action_id": "edit-preview", "action_type": "EDIT_PREVIEW", "label": "修改"},
                    {"action_id": "dismiss-preview", "action_type": "DISMISS", "label": "暂不创建"},
                ],
            },
        )
        await self._supersede_existing_previews(context.run)
        await self._complete_text(
            run_id,
            "计划预览已生成。确认后才会一次性创建正式计划、任务和通知。",
            cards=[card],
            validated=True,
        )

    async def _supersede_existing_previews(self, run: dict[str, object]) -> None:
        """新预览生成后，只更新旧聊天快照；不会触碰计划业务表。"""

        async with self.database.transaction() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT id,structured_content FROM messages WHERE conversation_id=:conversation "
                            "AND user_id=:user_id AND role='ASSISTANT' AND id<>:current "
                            "AND structured_content IS NOT NULL FOR UPDATE"
                        ),
                        {
                            "conversation": run["conversation_id"],
                            "user_id": run["user_id"],
                            "current": run["pending_response_message_id"],
                        },
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                structured = row["structured_content"]
                if not isinstance(structured, dict):
                    continue
                changed = False
                cards = structured.get("cards")
                for old_card in cards if isinstance(cards, list) else []:
                    if not isinstance(old_card, dict):
                        continue
                    payload = old_card.get("payload")
                    if (
                        old_card.get("card_type") == "PlanPreviewCard"
                        and isinstance(payload, dict)
                        and payload.get("status") == "READY"
                    ):
                        payload["status"] = "SUPERSEDED"
                        changed = True
                if changed:
                    await connection.execute(
                        text(
                            "UPDATE messages SET structured_content=CAST(:structured AS jsonb),"
                            "updated_at=now() WHERE id=:id"
                        ),
                        {"structured": _json(structured), "id": row["id"]},
                    )

    async def _load_planning_facts(
        self, context: RunContext, decision: IntentDecisionV1
    ) -> dict[str, object]:
        user_id = UUID(str(context.run["user_id"]))
        plans = list(await read_plan_context(self.database, user_id))
        tasks: list[dict[str, object]] = []
        if "TASK_CONTEXT" in decision.context_needs:
            tasks = list(
                await read_task_context(
                    self.database,
                    user_id,
                    datetime.now(UTC).date() - timedelta(days=7),
                    datetime.now(UTC).date() + timedelta(days=7),
                )
            )
        return {
            "request": context.user_input,
            "intent": decision.intent,
            "today": datetime.now(UTC).date().isoformat(),
            "plans": plans,
            "tasks": tasks,
        }

    async def _complete_text(
        self,
        run_id: UUID,
        content: str,
        *,
        cards: list[dict[str, object]] | None = None,
        validated: bool = False,
    ) -> None:
        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                )
            )
            if run is None or run["status"] != "RUNNING":
                return
            complete = self._complete_validated_run if validated else self._complete_run
            await complete(connection, run, content, cards=cards)

    async def _build_context(
        self,
        run_id: UUID,
        *,
        include_memories: bool = True,
        task: Literal["ROUTER", "GENERAL", "PLANNING", "RESEARCH"] = "GENERAL",
    ) -> RunContext:
        return await build_run_context(
            self._owner, run_id, include_memories=include_memories, task=task
        )
