from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from ai_butler.api.schemas import (
    TaskExecutionRequest,
)
from ai_butler.domain.errors import ButlerError, conflict, not_found

from .context import ButlerContext
from .shared import (
    _content_hash,
    _json,
    _row,
)
from .users import UserService


class PlanningService:
    def __init__(self, context: ButlerContext, users: UserService) -> None:
        self.database = context.database
        self.get_me = users.get_me

    async def dashboard(self, user_id: UUID, requested_date: date) -> dict[str, object]:
        plans = await self.list_plans(user_id)
        tasks = await self.list_tasks(user_id, requested_date, requested_date)
        plan_items = cast(list[dict[str, Any]], plans["items"])
        task_items = cast(list[dict[str, Any]], tasks["items"])
        done = sum(1 for task in task_items if task["status"] == "DONE")
        return {
            "date": requested_date,
            "timezone": (await self.get_me(user_id))["timezone"],
            "experience_state": "ACTIVE" if plan_items else "EMPTY",
            "butler": {
                "status": "ONLINE",
                "active_specialist_count": 1,
                "summary": f"今天有 {len(task_items) - done} 项任务待完成",
            },
            "plan_summary": {
                "total": len(plan_items),
                "active": sum(1 for plan in plan_items if plan["status"] == "ACTIVE"),
                "completed": sum(1 for plan in plan_items if plan["status"] == "COMPLETED"),
            },
            "task_summary": {
                "today_total": len(task_items),
                "today_done": done,
                "week_total": len(task_items),
                "week_done": done,
                "overloaded_minutes": 0,
            },
            "plans": plan_items,
            "today_tasks": task_items,
        }

    async def list_plans(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT p.id,p.goal_id,p.title,p.status,p.current_revision_id,r.start_date,r.end_date,"
                            "r.weekly_minutes,p.updated_at,g.goal_type,"
                            "COUNT(t.id) AS task_total,COUNT(t.id) FILTER(WHERE t.status='DONE') AS task_done "
                            "FROM plans p JOIN goals g ON g.id=p.goal_id "
                            "LEFT JOIN plan_revisions r ON r.id=p.current_revision_id "
                            "LEFT JOIN tasks t ON t.plan_id=p.id WHERE p.user_id=:user_id "
                            "AND p.status<>'DELETED' "
                            "GROUP BY p.id,r.id,g.goal_type ORDER BY p.updated_at DESC"
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
            items = []
            for row in rows:
                item = dict(row)
                total = int(item.pop("task_total"))
                completed = int(item.pop("task_done"))
                item["progress"] = {
                    "completed": completed,
                    "total": total,
                    "percent": round(completed * 100 / total) if total else 0,
                }
                items.append(item)
            return {"items": items, "next_cursor": None, "has_more": False}

    async def list_goals(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM goals WHERE user_id=:user_id AND status<>'DELETED' "
                            "ORDER BY updated_at DESC"
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
            return {"items": [dict(row) for row in rows], "next_cursor": None, "has_more": False}

    async def list_revisions(self, user_id: UUID, plan_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT id,plan_id,revision,status,objective_summary,start_date,end_date,weekly_minutes,"
                            "change_reason,approved_at,created_at FROM plan_revisions r "
                            "WHERE user_id=:user_id AND plan_id=:plan_id AND EXISTS "
                            "(SELECT 1 FROM plans p WHERE p.id=r.plan_id AND p.status<>'DELETED') "
                            "ORDER BY revision DESC"
                        ),
                        {"user_id": user_id, "plan_id": plan_id},
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                plan = await connection.execute(
                    text(
                        "SELECT 1 FROM plans WHERE id=:id AND user_id=:user_id "
                        "AND status<>'DELETED'"
                    ),
                    {"id": plan_id, "user_id": user_id},
                )
                if plan.first() is None:
                    raise not_found()
            return {"items": [dict(row) for row in rows], "next_cursor": None, "has_more": False}

    async def get_revision(
        self, user_id: UUID, plan_id: UUID, revision_id: UUID
    ) -> dict[str, object]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT r.* FROM plan_revisions r JOIN plans p ON p.id=r.plan_id "
                        "WHERE r.id=:id AND r.plan_id=:plan_id AND r.user_id=:user_id "
                        "AND p.status<>'DELETED'"
                    ),
                    {"id": revision_id, "plan_id": plan_id, "user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            return row

    async def get_plan(self, user_id: UUID, plan_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            plan = _row(
                await connection.execute(
                    text(
                        "SELECT p.*,g.title AS goal_title,g.goal_type,g.target_date,g.status AS goal_status "
                        "FROM plans p JOIN goals g ON g.id=p.goal_id WHERE p.id=:id "
                        "AND p.user_id=:user_id AND p.status<>'DELETED'"
                    ),
                    {"id": plan_id, "user_id": user_id},
                )
            )
            if plan is None:
                raise not_found()
            revision = _row(
                await connection.execute(
                    text("SELECT * FROM plan_revisions WHERE id=:id AND user_id=:user_id"),
                    {"id": plan["current_revision_id"], "user_id": user_id},
                )
            )
            return {
                "id": plan["id"],
                "goal": {
                    "id": plan["goal_id"],
                    "title": plan["goal_title"],
                    "goal_type": plan["goal_type"],
                    "target_date": plan["target_date"],
                    "status": plan["goal_status"],
                },
                "title": plan["title"],
                "status": plan["status"],
                "current_revision": revision,
                "updated_at": plan["updated_at"],
            }

    async def list_tasks(
        self, user_id: UUID, date_from: date | None, date_to: date | None
    ) -> dict[str, object]:
        date_from = date_from or datetime.now(UTC).date()
        date_to = date_to or date_from + timedelta(days=7)
        if (date_to - date_from).days > 93:
            raise ButlerError("INVALID_DATE_RANGE", "任务查询范围不能超过 93 天", 400)
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT t.*,p.title AS plan_title FROM tasks t JOIN plans p ON p.id=t.plan_id "
                            "WHERE t.user_id=:user_id AND p.status<>'DELETED' "
                            "AND t.scheduled_date BETWEEN :date_from AND :date_to "
                            "ORDER BY t.scheduled_date,t.priority,t.created_at"
                        ),
                        {"user_id": user_id, "date_from": date_from, "date_to": date_to},
                    )
                )
                .mappings()
                .all()
            )
            return {"items": [dict(row) for row in rows], "next_cursor": None, "has_more": False}

    async def get_task(self, user_id: UUID, task_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT t.*,p.title AS plan_title FROM tasks t JOIN plans p ON p.id=t.plan_id "
                        "WHERE t.id=:id AND t.user_id=:user_id AND p.status<>'DELETED'"
                    ),
                    {"id": task_id, "user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            return row

    async def execute_task(
        self, user_id: UUID, task_id: UUID, request: TaskExecutionRequest
    ) -> dict[str, object]:
        async with self.database.transaction() as connection:
            duplicate = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM task_executions WHERE user_id=:user_id AND client_execution_id=:client_id"
                    ),
                    {"user_id": user_id, "client_id": request.client_execution_id},
                )
            )
            task = _row(
                await connection.execute(
                    text(
                        "SELECT t.* FROM tasks t JOIN plans p ON p.id=t.plan_id "
                        "WHERE t.id=:id AND t.user_id=:user_id AND p.status<>'DELETED' FOR UPDATE OF t"
                    ),
                    {"id": task_id, "user_id": user_id},
                )
            )
            if task is None:
                raise not_found()
            if duplicate:
                return {"execution": duplicate, "task": task}
            execution_id = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO task_executions(id,user_id,task_id,client_execution_id,result,duration_minutes,"
                    "feedback,outcome_data,occurred_at) VALUES(:id,:user_id,:task_id,:client_id,:result,:duration,"
                    ":feedback,CAST(:outcome AS jsonb),:occurred_at)"
                ),
                {
                    "id": execution_id,
                    "user_id": user_id,
                    "task_id": task_id,
                    "client_id": request.client_execution_id,
                    "result": request.result,
                    "duration": request.duration_minutes,
                    "feedback": request.feedback,
                    "outcome": request.model_dump_json(include={"outcome_data"}),
                    "occurred_at": request.occurred_at,
                },
            )
            new_status = {
                "COMPLETED": "DONE",
                "SKIPPED": "SKIPPED",
                "PARTIAL": task["status"],
            }[request.result]
            await connection.execute(
                text(
                    "UPDATE tasks SET status=CAST(:status AS varchar),completed_at=CASE "
                    "WHEN CAST(:status AS varchar)='DONE' THEN :occurred ELSE completed_at END,"
                    "updated_at=now() WHERE id=:id"
                ),
                {"status": new_status, "occurred": request.occurred_at, "id": task_id},
            )
            return {
                "execution": {
                    "id": execution_id,
                    "task_id": task_id,
                    "result": request.result,
                    "duration_minutes": request.duration_minutes,
                    "occurred_at": request.occurred_at,
                },
                "task": {"id": task_id, "status": new_status},
            }

    async def delete_plan(self, user_id: UUID, plan_id: UUID, idempotency_key: str) -> None:
        """软删除计划并在同一事务停止任务、提醒与滚动排期。"""

        key_hash = _content_hash({"key": idempotency_key})
        request_hash = _content_hash({"plan_id": str(plan_id)})
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO request_idempotency_keys(id,user_id,scope,key_hash,request_hash) "
                    "VALUES(:id,:user_id,'PLAN_DELETE',:key_hash,:request_hash) "
                    "ON CONFLICT(user_id,scope,key_hash) DO NOTHING"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "key_hash": key_hash,
                    "request_hash": request_hash,
                },
            )
            reservation = _row(
                await connection.execute(
                    text(
                        "SELECT id,request_hash,response_data FROM request_idempotency_keys "
                        "WHERE user_id=:user_id AND scope='PLAN_DELETE' AND key_hash=:key FOR UPDATE"
                    ),
                    {"user_id": user_id, "key": key_hash},
                )
            )
            if reservation is None:
                raise ButlerError("IDEMPOTENCY_RESERVATION_FAILED", "无法锁定删除请求", 500)
            if reservation["request_hash"] != request_hash:
                raise conflict("IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他计划")
            if isinstance(reservation["response_data"], dict):
                return
            plan = _row(
                await connection.execute(
                    text("SELECT * FROM plans WHERE id=:id AND user_id=:user_id FOR UPDATE"),
                    {"id": plan_id, "user_id": user_id},
                )
            )
            if plan is None:
                raise not_found()
            await connection.execute(
                text("SELECT id FROM goals WHERE id=:id AND user_id=:user_id FOR UPDATE"),
                {"id": plan["goal_id"], "user_id": user_id},
            )
            await connection.execute(
                text("SELECT id FROM plan_schedule_watermarks WHERE plan_id=:plan_id FOR UPDATE"),
                {"plan_id": plan_id},
            )
            if plan["status"] != "DELETED":
                await connection.execute(
                    text(
                        "UPDATE plans SET status='DELETED',deleted_at=now(),"
                        "deleted_reason='USER_REQUESTED',updated_at=now() WHERE id=:id"
                    ),
                    {"id": plan_id},
                )
                await connection.execute(
                    text(
                        "UPDATE tasks SET status='CANCELLED',cancellation_reason='PLAN_DELETED',"
                        "updated_at=now() WHERE plan_id=:id AND status IN ('TODO','DOING')"
                    ),
                    {"id": plan_id},
                )
                await connection.execute(
                    text(
                        "UPDATE notification_jobs SET status='CANCELLED',updated_at=now() "
                        "WHERE task_id IN (SELECT id FROM tasks WHERE plan_id=:id) "
                        "AND status IN ('PENDING','RETRY','RUNNING')"
                    ),
                    {"id": plan_id},
                )
                await connection.execute(
                    text("DELETE FROM plan_schedule_watermarks WHERE plan_id=:id"),
                    {"id": plan_id},
                )
                remaining = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM plans WHERE goal_id=:goal_id "
                                "AND status<>'DELETED'"
                            ),
                            {"goal_id": plan["goal_id"]},
                        )
                    ).scalar_one()
                )
                if remaining == 0:
                    await connection.execute(
                        text(
                            "UPDATE goals SET status='DELETED',deleted_at=now(),"
                            "deleted_reason='LAST_PLAN_DELETED',updated_at=now() WHERE id=:id"
                        ),
                        {"id": plan["goal_id"]},
                    )
            await connection.execute(
                text(
                    "UPDATE request_idempotency_keys SET response_data=CAST(:response AS jsonb),"
                    "updated_at=now() WHERE id=:id"
                ),
                {"response": _json({"plan_id": str(plan_id)}), "id": reservation["id"]},
            )
