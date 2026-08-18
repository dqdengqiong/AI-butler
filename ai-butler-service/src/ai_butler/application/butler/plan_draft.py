"""组合计划草案、PlanCard 1.1 与目标选择中断。"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.agent.availability import AvailabilityInterpretationV1
from ai_butler.agent.contracts import PlanScopeV1
from ai_butler.agent.evidence import NumberedEvidence
from ai_butler.agent.runtime import DEFAULT_CAPABILITY_REGISTRY

from .bootstrap import BootstrapService
from .events import EventService
from .evidence_execution import EvidenceExecutionService
from .shared import _json
from .support import draft_tasks_for_availability, safe_summary


class PlanDraftService:
    def __init__(
        self,
        events: EventService,
        evidence: EvidenceExecutionService,
        bootstrap: BootstrapService,
    ) -> None:
        self._append_event = events._append_event
        self._persist_evidence = evidence._persist_evidence
        self._ensure_synthetic_source = bootstrap._ensure_synthetic_source

    async def _create_plan_draft(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        content: str,
        evidence: tuple[NumberedEvidence, ...],
        availability: AvailabilityInterpretationV1,
        scope: PlanScopeV1,
    ) -> None:
        DEFAULT_CAPABILITY_REGISTRY.require("plan_draft_write", "Planner", approved=False)
        if any(item.result.source_type == "KNOWLEDGE" for item in evidence):
            await self._ensure_synthetic_source(connection)
        objectives = self._work_item_objectives(content)
        existing_plans = [
            dict(item)
            for item in (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM plans WHERE user_id=:user_id AND status='ACTIVE' "
                            "ORDER BY created_at,id"
                        ),
                        {"user_id": run["user_id"]},
                    )
                )
                .mappings()
                .all()
            )
        ]
        target_plan_id = await self._trusted_target_plan_id(connection, run)
        target_plan = next(
            (plan for plan in existing_plans if UUID(str(plan["id"])) == target_plan_id),
            None,
        )
        if len(objectives) == 1 and len(existing_plans) > 1 and target_plan is None:
            await self._interrupt_for_plan_selection(connection, run, existing_plans, availability)
            return
        adjusting = len(objectives) == 1 and (len(existing_plans) == 1 or target_plan is not None)
        mode = (
            "SINGLE_PLAN_ADJUST"
            if adjusting
            else "BUNDLE_CREATE"
            if len(objectives) >= 2
            else "SINGLE_PLAN_CREATE"
        )
        start = scope.start_date
        end = scope.target_date
        capacity = int((availability.weekly_minutes or 0) * 0.85)
        weekly_minutes = max(30, capacity // len(objectives))
        approval_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO approval_decisions(id,user_id,agent_run_id) VALUES(:id,:user_id,:run_id)"
            ),
            {"id": approval_id, "user_id": run["user_id"], "run_id": run["id"]},
        )
        item_refs: list[dict[str, object]] = []
        plan_payloads: list[dict[str, object]] = []
        revision_ids: list[UUID] = []
        for index, objective in enumerate(objectives):
            existing_plan = (target_plan or existing_plans[0]) if adjusting else None
            if existing_plan is None:
                goal_id, plan_id, revision_number, expected_revision = uuid4(), uuid4(), 1, None
                title = safe_summary(objective)[:80] or f"备考计划 {index + 1}"
                await connection.execute(
                    text(
                        "INSERT INTO goals(id,user_id,goal_type,title,status,target_date) "
                        "VALUES(:id,:user_id,'CIVIL_SERVICE_EXAM',:title,'DRAFT',:target_date)"
                    ),
                    {
                        "id": goal_id,
                        "user_id": run["user_id"],
                        "title": title,
                        "target_date": scope.target_date,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO plans(id,user_id,goal_id,title,status) "
                        "VALUES(:id,:user_id,:goal_id,:title,'DRAFT')"
                    ),
                    {
                        "id": plan_id,
                        "user_id": run["user_id"],
                        "goal_id": goal_id,
                        "title": title,
                    },
                )
            else:
                plan_id = existing_plan["id"]
                expected_revision = existing_plan["current_revision_id"]
                await connection.execute(
                    text(
                        "UPDATE goals SET target_date=:target_date,updated_at=now() "
                        "WHERE id=:goal_id AND user_id=:user_id"
                    ),
                    {
                        "target_date": scope.target_date,
                        "goal_id": existing_plan["goal_id"],
                        "user_id": run["user_id"],
                    },
                )
                revision_number = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT COALESCE(MAX(revision),0)+1 FROM plan_revisions WHERE plan_id=:id"
                            ),
                            {"id": plan_id},
                        )
                    ).scalar_one()
                )
            revision_id = uuid4()
            revision_ids.append(revision_id)
            work_item_id = f"work-{index + 1}-{revision_id}"
            tasks = self._scale_tasks(
                draft_tasks_for_availability(start, availability), weekly_minutes
            )
            summary = f"备考计划：{safe_summary(objective)}；{availability.summary}"
            await connection.execute(
                text(
                    "INSERT INTO plan_revisions(id,plan_id,user_id,agent_run_id,revision,status,objective_summary,"
                    "start_date,end_date,weekly_minutes,change_reason,content) "
                    "VALUES(:id,:plan_id,:user_id,:run_id,:revision,'PENDING_APPROVAL',:summary,:start,:end,"
                    ":weekly_minutes,:reason,CAST(:content AS jsonb))"
                ),
                {
                    "id": revision_id,
                    "plan_id": plan_id,
                    "user_id": run["user_id"],
                    "run_id": run["id"],
                    "revision": revision_number,
                    "summary": summary,
                    "start": start,
                    "end": end,
                    "weekly_minutes": weekly_minutes,
                    "reason": safe_summary(content),
                    "content": _json(
                        {
                            "tasks": tasks,
                            "availability": availability.model_dump(mode="json"),
                            "plan_scope": scope.model_dump(mode="json"),
                            "work_item_id": work_item_id,
                        }
                    ),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO approval_decision_items(id,approval_id,plan_id,plan_revision_id,"
                    "expected_current_revision_id,work_item_id) "
                    "VALUES(:id,:approval,:plan,:revision,:expected,:work_item_id)"
                ),
                {
                    "id": uuid4(),
                    "approval": approval_id,
                    "plan": plan_id,
                    "revision": revision_id,
                    "expected": expected_revision,
                    "work_item_id": work_item_id,
                },
            )
            item_refs.append(
                {
                    "work_item_id": work_item_id,
                    "plan_id": str(plan_id),
                    "plan_revision_id": str(revision_id),
                    "expected_current_revision_id": (
                        str(expected_revision) if expected_revision else None
                    ),
                }
            )
            plan_payloads.append(
                {
                    "work_item_id": work_item_id,
                    "plan_id": str(plan_id),
                    "plan_revision_id": str(revision_id),
                    "title": safe_summary(objective)[:80],
                    "objective_summary": summary,
                    "weekly_minutes": weekly_minutes,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                }
            )
        source_card = await self._persist_evidence(
            connection,
            run,
            evidence,
            claim_text="本计划参考了检索结果中的公务员备考科目与训练建议",
            plan_revision_id=revision_ids[0],
        )
        card = {
            "schema_version": "1.1",
            "card_id": str(uuid4()),
            "card_type": "PlanCard",
            "entity_refs": {
                "approval_id": str(approval_id),
                "approval_version": 1,
                "items": item_refs,
            },
            "payload": {
                "mode": mode,
                "title": "公务员备考 · 计划草案",
                "plans": plan_payloads,
                "total_weekly_minutes": weekly_minutes * len(objectives),
                "available_weekly_minutes": int(availability.weekly_minutes or 0),
                "warnings": (["当前为合成离线来源，不代表真实考试公告。"] if evidence else []),
            },
            "actions": [
                {"action_id": "approve", "action_type": "APPROVE", "label": "确认计划"},
                {"action_id": "edit", "action_type": "EDIT", "label": "继续修改"},
                {"action_id": "reject", "action_type": "REJECT", "label": "拒绝"},
            ],
        }
        citation_marks = "".join(f"[{item.index}]" for item in evidence)
        response = (
            f"我已参考检索来源生成计划草案。{citation_marks}请使用卡片按钮确认、修改或拒绝。"
            if evidence
            else "我已生成计划草案。请使用卡片按钮确认、修改或拒绝。"
        )
        cards = [card, *([source_card] if source_card else [])]
        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,"
                "structured_content=CAST(:cards AS jsonb),updated_at=now() WHERE id=:id"
            ),
            {
                "content": response,
                "cards": _json({"cards": cards}),
                "id": run["pending_response_message_id"],
            },
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='AWAITING_APPROVAL',updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "message.completed",
            {
                "message_id": str(run["pending_response_message_id"]),
                "content": response,
                "cards": cards,
            },
            run["attempt"],
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "interrupt",
            {"type": "APPROVAL", "approval_id": str(approval_id), "approval_version": 1},
            run["attempt"],
        )

    @staticmethod
    def _work_item_objectives(content: str) -> tuple[str, ...]:
        items = tuple(
            item.strip(" ，,。")
            for item in re.split(r"(?:同时|另外|以及|并且|[；;])", content)
            if item.strip(" ，,。")
        )
        return items if len(items) >= 2 else (content.strip(),)

    @staticmethod
    def _scale_tasks(
        tasks: list[dict[str, object]], weekly_minutes: int
    ) -> list[dict[str, object]]:
        total = sum(int(str(item["minutes"])) for item in tasks)
        if total <= weekly_minutes:
            return tasks
        ratio = weekly_minutes / total
        return [
            {**item, "minutes": max(10, int(int(str(item["minutes"])) * ratio))} for item in tasks
        ]

    @staticmethod
    async def _trusted_target_plan_id(
        connection: AsyncConnection, run: dict[str, Any]
    ) -> UUID | None:
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT structured_content FROM messages WHERE agent_run_id=:run_id "
                        "AND role='USER' ORDER BY created_at DESC,id DESC"
                    ),
                    {"run_id": run["id"]},
                )
            )
            .scalars()
            .all()
        )
        for structured in rows:
            options = structured.get("selected_options", []) if isinstance(structured, dict) else []
            for option in options if isinstance(options, list) else []:
                if not isinstance(option, dict) or option.get("target_plan_id") is None:
                    continue
                try:
                    return UUID(str(option["target_plan_id"]))
                except ValueError:
                    return None
        return None

    async def _interrupt_for_plan_selection(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        plans: list[dict[str, Any]],
        availability: AvailabilityInterpretationV1,
    ) -> None:
        content = "你有多个生效计划，请先选择要调整的计划。"
        card = {
            "schema_version": "1.0",
            "card_id": str(uuid4()),
            "card_type": "SelectionCard",
            "entity_refs": {},
            "payload": {
                "phase": "SELECT_TARGET_PLAN",
                "question": content,
                "description": "选择是必需的；自由文本不会产生审批或发布副作用。",
                "input_mode": "NATURAL_LANGUAGE",
                "options": [
                    {
                        "id": str(plan["id"]),
                        "label": str(plan["title"]),
                        "target_plan_id": str(plan["id"]),
                        "availability": availability.model_dump(mode="json"),
                    }
                    for plan in plans
                ],
            },
            "actions": [
                {
                    "action_id": "submit-selection",
                    "action_type": "SUBMIT_SELECTION",
                    "label": "选择计划",
                }
            ],
        }
        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,"
                "structured_content=CAST(:cards AS jsonb),updated_at=now() WHERE id=:id"
            ),
            {
                "content": content,
                "cards": _json({"cards": [card]}),
                "id": run["pending_response_message_id"],
            },
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='AWAITING_INPUT',updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "interrupt",
            {"type": "INPUT", "message": content, "cards": [card]},
            run["attempt"],
        )
