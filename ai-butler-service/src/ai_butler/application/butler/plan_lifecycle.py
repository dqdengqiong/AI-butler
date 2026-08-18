"""不可变计划审批、发布及任务物化生命周期。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.agent.contracts import ExecutorResultV1, TaskDraftV1
from ai_butler.agent.runtime import DEFAULT_CAPABILITY_REGISTRY
from ai_butler.domain.errors import conflict

from .events import EventService
from .shared import _json, _row


class PlanLifecycleService:
    def __init__(self, events: EventService) -> None:
        self._append_event = events._append_event

    async def _regenerate_approval(
        self, connection: AsyncConnection, run: dict[str, Any], approval: dict[str, Any]
    ) -> None:
        items = (
            (
                await connection.execute(
                    text("SELECT * FROM approval_decision_items WHERE approval_id=:id"),
                    {"id": approval["id"]},
                )
            )
            .mappings()
            .all()
        )
        new_approval_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO approval_decisions(id,user_id,agent_run_id) "
                "VALUES(:id,:user_id,:run_id)"
            ),
            {"id": new_approval_id, "user_id": run["user_id"], "run_id": run["id"]},
        )
        new_refs: list[dict[str, object]] = []
        updated_plans: dict[str, dict[str, object]] = {}
        for item in items:
            revision = _row(
                await connection.execute(
                    text("SELECT * FROM plan_revisions WHERE id=:id FOR UPDATE"),
                    {"id": item["plan_revision_id"]},
                )
            )
            if revision is None:
                raise conflict("PLAN_REVISION_CONFLICT", "计划草案不存在")
            next_weekly_minutes = max(1, int(revision["weekly_minutes"]) * 5 // 6)
            new_revision_id = uuid4()
            revision_number = int(
                (
                    await connection.execute(
                        text(
                            "SELECT COALESCE(MAX(revision),0)+1 FROM plan_revisions WHERE plan_id=:id"
                        ),
                        {"id": item["plan_id"]},
                    )
                ).scalar_one()
            )
            content = dict(revision["content"] or {})
            tasks = content.get("tasks")
            if isinstance(tasks, list):
                content["tasks"] = self._scale_tasks(
                    [dict(task) for task in tasks if isinstance(task, dict)],
                    next_weekly_minutes,
                )
            await connection.execute(
                text(
                    "INSERT INTO plan_revisions(id,plan_id,user_id,agent_run_id,revision,status,"
                    "objective_summary,start_date,end_date,weekly_minutes,change_reason,content) "
                    "VALUES(:id,:plan_id,:user_id,:run_id,:revision,'PENDING_APPROVAL',:summary,"
                    ":start,:end,:weekly_minutes,:reason,CAST(:content AS jsonb))"
                ),
                {
                    "id": new_revision_id,
                    "plan_id": item["plan_id"],
                    "user_id": run["user_id"],
                    "run_id": run["id"],
                    "revision": revision_number,
                    "summary": "已按反馈降低负荷的四周公务员备考计划",
                    "start": revision["start_date"],
                    "end": revision["end_date"],
                    "weekly_minutes": next_weekly_minutes,
                    "reason": approval["feedback"],
                    "content": _json(content),
                },
            )
            await connection.execute(
                text(
                    "UPDATE plan_revisions SET status='SUPERSEDED' "
                    "WHERE id=:id AND status='PENDING_APPROVAL'"
                ),
                {"id": revision["id"]},
            )
            await connection.execute(
                text(
                    "INSERT INTO approval_decision_items(id,approval_id,plan_id,plan_revision_id,"
                    "expected_current_revision_id,work_item_id) "
                    "VALUES(:id,:approval,:plan,:revision,:expected,:work_item_id)"
                ),
                {
                    "id": uuid4(),
                    "approval": new_approval_id,
                    "plan": item["plan_id"],
                    "revision": new_revision_id,
                    "expected": item["expected_current_revision_id"],
                    "work_item_id": item["work_item_id"],
                },
            )
            new_refs.append(
                {
                    "work_item_id": item["work_item_id"],
                    "plan_id": str(item["plan_id"]),
                    "plan_revision_id": str(new_revision_id),
                    "expected_current_revision_id": (
                        str(item["expected_current_revision_id"])
                        if item["expected_current_revision_id"]
                        else None
                    ),
                }
            )
            updated_plans[str(item["plan_id"])] = {
                "plan_revision_id": str(new_revision_id),
                "objective_summary": "已按反馈降低负荷的四周公务员备考计划",
                "weekly_minutes": next_weekly_minutes,
            }
        response = "我已根据反馈更新草案，请再次使用计划卡确认。"
        message = _row(
            await connection.execute(
                text("SELECT structured_content FROM messages WHERE id=:id FOR UPDATE"),
                {"id": run["pending_response_message_id"]},
            )
        )
        structured = (message or {}).get("structured_content")
        cards = structured.get("cards", []) if isinstance(structured, dict) else []
        for card in cards:
            if not isinstance(card, dict) or card.get("card_type") != "PlanCard":
                continue
            refs = card.get("entity_refs")
            if not isinstance(refs, dict) or str(refs.get("approval_id")) != str(approval["id"]):
                continue
            refs.update(
                {
                    "approval_id": str(new_approval_id),
                    "approval_version": 1,
                    "approval_status": "PENDING",
                    "items": new_refs,
                }
            )
            payload = card.get("payload")
            if isinstance(payload, dict):
                plan_cards = payload.get("plans", [])
                for plan in plan_cards if isinstance(plan_cards, list) else []:
                    if isinstance(plan, dict) and (
                        update := updated_plans.get(str(plan.get("plan_id")))
                    ):
                        plan.update(update)
                payload["total_weekly_minutes"] = sum(
                    int(plan.get("weekly_minutes", 0))
                    for plan in plan_cards
                    if isinstance(plan, dict)
                )
        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,"
                "structured_content=CAST(:cards AS jsonb),updated_at=now() WHERE id=:id"
            ),
            {
                "content": response,
                "cards": _json(structured if isinstance(structured, dict) else {}),
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
            "interrupt",
            {"type": "APPROVAL", "approval_id": str(new_approval_id), "approval_version": 1},
            run["attempt"],
        )

    async def _publish_revision(
        self, connection: AsyncConnection, user_id: UUID, item: dict[str, Any]
    ) -> None:
        DEFAULT_CAPABILITY_REGISTRY.require("plan_publish", "Executor", approved=True)
        revision = _row(
            await connection.execute(
                text("SELECT * FROM plan_revisions WHERE id=:id AND user_id=:user_id"),
                {"id": item["plan_revision_id"], "user_id": user_id},
            )
        )
        if revision is None:
            raise conflict("PLAN_REVISION_CONFLICT", "计划草案不存在")
        await connection.execute(
            text(
                "UPDATE plan_revisions SET status='SUPERSEDED' WHERE plan_id=:plan AND status='APPROVED'"
            ),
            {"plan": item["plan_id"]},
        )
        await connection.execute(
            text("UPDATE plan_revisions SET status='APPROVED',approved_at=now() WHERE id=:id"),
            {"id": revision["id"]},
        )
        await connection.execute(
            text(
                "UPDATE plans SET current_revision_id=:revision,status='ACTIVE',updated_at=now() WHERE id=:plan"
            ),
            {"revision": revision["id"], "plan": item["plan_id"]},
        )
        await connection.execute(
            text(
                "UPDATE goals SET status='ACTIVE',updated_at=now() "
                "WHERE id=(SELECT goal_id FROM plans WHERE id=:plan)"
            ),
            {"plan": item["plan_id"]},
        )
        await connection.execute(
            text(
                "UPDATE tasks SET status='CANCELLED',cancellation_reason='REVISION_SUPERSEDED',"
                "updated_at=now() WHERE plan_id=:plan AND plan_revision_id<>:revision "
                "AND status IN ('TODO','DOING') AND scheduled_date >= ("
                "SELECT (now() AT TIME ZONE timezone)::date FROM users WHERE id=:user_id)"
            ),
            {"plan": item["plan_id"], "revision": revision["id"], "user_id": user_id},
        )

    async def _materialize_approval(
        self, connection: AsyncConnection, user_id: UUID, approval_id: UUID
    ) -> None:
        DEFAULT_CAPABILITY_REGISTRY.require("task_materialize", "Executor", approved=True)
        items = (
            (
                await connection.execute(
                    text(
                        "SELECT i.* FROM approval_decision_items i "
                        "JOIN approval_decisions a ON a.id=i.approval_id "
                        "WHERE i.approval_id=:approval AND a.user_id=:user_id "
                        "AND a.status='APPROVED' ORDER BY i.plan_id FOR UPDATE"
                    ),
                    {"approval": approval_id, "user_id": user_id},
                )
            )
            .mappings()
            .all()
        )
        for item in items:
            revision = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM plan_revisions WHERE id=:id AND user_id=:user_id "
                        "AND status='APPROVED'"
                    ),
                    {"id": item["plan_revision_id"], "user_id": user_id},
                )
            )
            if revision is None:
                raise conflict("PLAN_REVISION_CONFLICT", "计划发布事实已变化")
            for index, task in enumerate((revision["content"] or {}).get("tasks", [])):
                task_key = f"template:{index}:day:{int(task['day_offset'])}"
                task_id = uuid5(UUID(str(revision["id"])), task_key)
                scheduled = revision["start_date"] + timedelta(days=int(task["day_offset"]))
                if scheduled > datetime.now(UTC).date() + timedelta(days=6):
                    continue
                await connection.execute(
                    text(
                        "INSERT INTO tasks(id,user_id,plan_id,plan_revision_id,task_key,title,"
                        "scheduled_date,expected_minutes) VALUES(:id,:user_id,:plan,:revision,:task_key,"
                        ":title,:date,:minutes) ON CONFLICT(plan_revision_id,task_key) DO NOTHING"
                    ),
                    {
                        "id": task_id,
                        "user_id": user_id,
                        "plan": item["plan_id"],
                        "revision": revision["id"],
                        "task_key": task_key,
                        "title": task["title"],
                        "date": scheduled,
                        "minutes": task["minutes"],
                    },
                )

    async def _materialize_model_approval(
        self,
        connection: AsyncConnection,
        user_id: UUID,
        approval_id: UUID,
        schedules: dict[UUID, ExecutorResultV1],
    ) -> None:
        """重新锁定已批准 revision，验证模型候选后幂等写入七日任务。"""

        DEFAULT_CAPABILITY_REGISTRY.require("task_materialize", "Executor", approved=True)
        today = datetime.now(UTC).date()
        horizon = today + timedelta(days=6)
        items = (
            (
                await connection.execute(
                    text(
                        "SELECT i.* FROM approval_decision_items i "
                        "JOIN approval_decisions a ON a.id=i.approval_id "
                        "WHERE i.approval_id=:approval AND a.user_id=:user_id "
                        "AND a.status='APPROVED' ORDER BY i.plan_id FOR UPDATE"
                    ),
                    {"approval": approval_id, "user_id": user_id},
                )
            )
            .mappings()
            .all()
        )
        if not items:
            raise conflict("PLAN_REVISION_CONFLICT", "审批或计划版本已变化")
        for row in items:
            item = dict(row)
            revision_id = UUID(str(item["plan_revision_id"]))
            schedule = schedules.get(revision_id)
            if schedule is None:
                raise conflict("PLAN_REVISION_CONFLICT", "任务排期缺少已批准计划")
            revision = _row(
                await connection.execute(
                    text(
                        "SELECT r.* FROM plan_revisions r JOIN plans p ON p.id=r.plan_id "
                        "WHERE r.id=:id AND r.user_id=:user_id AND r.status='APPROVED' "
                        "AND p.current_revision_id=r.id FOR UPDATE"
                    ),
                    {"id": revision_id, "user_id": user_id},
                )
            )
            if revision is None:
                raise conflict("PLAN_REVISION_CONFLICT", "计划发布事实已变化")
            template_rows = [
                dict(value)
                for value in (
                    (
                        await connection.execute(
                            text(
                                "SELECT t.*,s.sequence AS stage_sequence,t.schedule_rule->>'stage_key' "
                                "AS stage_key FROM plan_task_templates t JOIN plan_stages s "
                                "ON s.id=t.stage_id WHERE t.plan_revision_id=:revision"
                            ),
                            {"revision": revision_id},
                        )
                    )
                    .mappings()
                    .all()
                )
            ]
            templates = {str(value["template_key"]): value for value in template_rows}
            availability = (revision.get("content") or {}).get("availability", {})
            weekly_available = int(availability.get("weekly_minutes") or revision["weekly_minutes"])
            total_capacity = max(1, int(weekly_available * 0.85))
            daily_capacity = {
                int(window["day_of_week"]): int(int(window["available_minutes"]) * 0.85)
                for window in availability.get("windows", [])
                if isinstance(window, dict)
                and window.get("day_of_week") is not None
                and window.get("available_minutes") is not None
            }
            excluded_days = {int(value) for value in availability.get("excluded_days", [])}
            total_minutes = 0
            daily_minutes: dict[date, int] = {}
            seen_keys: set[str] = set()
            validated: list[tuple[TaskDraftV1, dict[str, Any], str]] = []
            for draft in schedule.task_drafts:
                template = templates.get(draft.template_key)
                if template is None:
                    raise conflict("TASK_TEMPLATE_INVALID", "任务引用了未批准的模板")
                if draft.stage_key != str(template.get("stage_key") or ""):
                    raise conflict("TASK_TEMPLATE_INVALID", "任务引用了错误的计划阶段")
                if not today <= draft.scheduled_date <= horizon:
                    raise conflict("TASK_DATE_INVALID", "任务日期不在未来七天范围内")
                if draft.scheduled_date.isoweekday() in excluded_days:
                    raise conflict("TASK_DATE_INVALID", "任务日期不在用户可用时间内")
                if draft.expected_minutes > int(template["expected_minutes"]):
                    raise conflict("TASK_LOAD_INVALID", "任务时长超过已批准模板")
                task_key = f"template:{draft.template_key}:date:{draft.scheduled_date.isoformat()}"
                if task_key in seen_keys:
                    raise conflict("TASK_KEY_DUPLICATE", "任务稳定键重复")
                seen_keys.add(task_key)
                total_minutes += draft.expected_minutes
                daily_minutes[draft.scheduled_date] = (
                    daily_minutes.get(draft.scheduled_date, 0) + draft.expected_minutes
                )
                validated.append((draft, template, task_key))
            if total_minutes > total_capacity:
                raise conflict("TASK_LOAD_INVALID", "七日任务总负荷超过可用时间的 85%")
            for scheduled, minutes in daily_minutes.items():
                if daily_capacity and minutes > daily_capacity.get(scheduled.isoweekday(), 0):
                    raise conflict("TASK_LOAD_INVALID", "单日任务负荷超过可用时间的 85%")
            for draft, template, task_key in validated:
                task_id = uuid5(revision_id, task_key)
                await connection.execute(
                    text(
                        "INSERT INTO tasks(id,user_id,plan_id,plan_revision_id,task_key,title,"
                        "scheduled_date,expected_minutes,priority) VALUES(:id,:user_id,:plan,:revision,"
                        ":task_key,:title,:date,:minutes,:priority) "
                        "ON CONFLICT(plan_revision_id,task_key) DO NOTHING"
                    ),
                    {
                        "id": task_id,
                        "user_id": user_id,
                        "plan": item["plan_id"],
                        "revision": revision_id,
                        "task_key": task_key,
                        "title": template["title"],
                        "date": draft.scheduled_date,
                        "minutes": draft.expected_minutes,
                        "priority": int((template["schedule_rule"] or {}).get("priority", 3)),
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO notification_jobs(id,user_id,task_id,event_type,channel,scheduled_at,"
                        "status,idempotency_key) VALUES(:id,:user_id,:task,'TASK_REMINDER','IN_APP',"
                        ":scheduled,'PENDING',:key) ON CONFLICT(idempotency_key) DO NOTHING"
                    ),
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "task": task_id,
                        "scheduled": datetime.combine(
                            draft.scheduled_date, datetime.min.time(), UTC
                        ),
                        "key": f"task-reminder:{task_id}:in-app",
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO notification_jobs(id,user_id,task_id,event_type,channel,scheduled_at,"
                        "status,idempotency_key) VALUES(:id,:user_id,:task,'TASK_REMINDER','IN_APP',"
                        ":scheduled,'PENDING',:key) ON CONFLICT(idempotency_key) DO NOTHING"
                    ),
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "task": task_id,
                        "scheduled": datetime.combine(scheduled, datetime.min.time(), UTC),
                        "key": f"task-reminder:{task_id}:in-app",
                    },
                )

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
