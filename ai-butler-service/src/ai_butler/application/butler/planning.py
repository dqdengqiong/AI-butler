from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from ai_butler.api.schemas import (
    TaskExecutionRequest,
)
from ai_butler.domain.errors import ButlerError, not_found

from .context import ButlerContext
from .shared import (
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
                        text("SELECT * FROM goals WHERE user_id=:user_id ORDER BY updated_at DESC"),
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
                            "change_reason,approved_at,created_at FROM plan_revisions "
                            "WHERE user_id=:user_id AND plan_id=:plan_id ORDER BY revision DESC"
                        ),
                        {"user_id": user_id, "plan_id": plan_id},
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                plan = await connection.execute(
                    text("SELECT 1 FROM plans WHERE id=:id AND user_id=:user_id"),
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
                        "SELECT * FROM plan_revisions WHERE id=:id AND plan_id=:plan_id AND user_id=:user_id"
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
                        "FROM plans p JOIN goals g ON g.id=p.goal_id WHERE p.id=:id AND p.user_id=:user_id"
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
                            "WHERE t.user_id=:user_id AND t.scheduled_date BETWEEN :date_from AND :date_to "
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
                        "WHERE t.id=:id AND t.user_id=:user_id"
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
                    text("SELECT * FROM tasks WHERE id=:id AND user_id=:user_id FOR UPDATE"),
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
