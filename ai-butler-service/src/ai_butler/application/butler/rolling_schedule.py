"""计划未来七日窗口的确定性滚动物化。"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import text

from ai_butler.infrastructure.database import AsyncDatabase
from ai_butler.tools import DEFAULT_TOOL_REGISTRY, schedule_plan_window

from .shared import _json, _row


class RollingScheduleMixin:
    """为 Scheduler 提供单计划滚动排期职责。"""

    database: AsyncDatabase

    async def _materialize_one_plan_window(self) -> bool:
        """在一个事务中将一个 ACTIVE 计划补齐到用户本地未来第七天。"""

        DEFAULT_TOOL_REGISTRY.require("schedule_plan_window", "Scheduler")
        async with self.database.transaction() as connection:
            watermark = _row(
                await connection.execute(
                    text(
                        "SELECT w.*,r.end_date,r.content,u.timezone FROM plan_schedule_watermarks w "
                        "JOIN plans p ON p.id=w.plan_id AND p.status='ACTIVE' "
                        "JOIN plan_revisions r ON r.id=w.plan_revision_id AND r.status='APPROVED' "
                        "JOIN users u ON u.id=w.user_id AND u.status='ACTIVE' "
                        "WHERE w.materialized_through < LEAST(r.end_date,"
                        "(now() AT TIME ZONE u.timezone)::date + 6) "
                        "ORDER BY w.materialized_through,w.plan_id "
                        "FOR UPDATE OF w SKIP LOCKED LIMIT 1"
                    )
                )
            )
            if watermark is None:
                return False
            timezone_name = str(watermark["timezone"])
            local_today = datetime.now(ZoneInfo(timezone_name)).date()
            desired = min(watermark["end_date"], local_today + timedelta(days=6))
            content = watermark["content"] if isinstance(watermark["content"], dict) else {}
            availability = content.get("availability")
            if not isinstance(availability, dict):
                raise ValueError("approved revision is missing normalized availability")
            stages = tuple(
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT ps.stage_key,ps.start_date,ps.end_date "
                            "FROM plan_stages ps WHERE ps.plan_revision_id=:revision ORDER BY ps.sequence"
                        ),
                        {"revision": watermark["plan_revision_id"]},
                    )
                ).mappings()
            )
            templates = tuple(
                {
                    "stage_key": row["stage_key"],
                    "template_key": row["template_key"],
                    "title": row["title"],
                    "expected_minutes": row["expected_minutes"],
                    "priority": (row["schedule_rule"] or {}).get("priority", 3),
                    "frequency": (row["schedule_rule"] or {}).get("frequency", {}),
                }
                for row in (
                    await connection.execute(
                        text(
                            "SELECT ps.stage_key,t.template_key,t.title,t.expected_minutes,"
                            "t.schedule_rule FROM plan_task_templates t JOIN plan_stages ps "
                            "ON ps.id=t.stage_id WHERE t.plan_revision_id=:revision "
                            "ORDER BY t.sequence,t.template_key"
                        ),
                        {"revision": watermark["plan_revision_id"]},
                    )
                ).mappings()
            )
            cursor = watermark["materialized_through"] + timedelta(days=1)
            while cursor <= desired:
                week_start = cursor - timedelta(days=cursor.isoweekday() - 1)
                week_end = week_start + timedelta(days=6)
                existing = tuple(
                    dict(row)
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT task_key,split_part(task_key,':',1) AS template_key,"
                                "scheduled_date,expected_minutes FROM tasks "
                                "WHERE plan_revision_id=:revision AND scheduled_date BETWEEN :start AND :end "
                                "AND status<>'CANCELLED'"
                            ),
                            {
                                "revision": watermark["plan_revision_id"],
                                "start": week_start,
                                "end": week_end,
                            },
                        )
                    ).mappings()
                )
                scheduled, unscheduled = schedule_plan_window(
                    revision_ref=str(watermark["plan_revision_id"]),
                    templates=templates,
                    stages=stages,
                    availability=availability,
                    window_start=cursor,
                    window_end=cursor,
                    existing=existing,
                )
                for item in scheduled:
                    task_id = uuid5(UUID(str(watermark["plan_revision_id"])), item.task_key)
                    inserted = (
                        await connection.execute(
                            text(
                                "INSERT INTO tasks(id,user_id,plan_id,plan_revision_id,task_key,title,"
                                "scheduled_date,expected_minutes,priority,status) VALUES(:id,:user_id,"
                                ":plan_id,:revision,:key,:title,:date,:minutes,:priority,'TODO') "
                                "ON CONFLICT(plan_revision_id,task_key) DO NOTHING RETURNING id"
                            ),
                            {
                                "id": task_id,
                                "user_id": watermark["user_id"],
                                "plan_id": watermark["plan_id"],
                                "revision": watermark["plan_revision_id"],
                                "key": item.task_key,
                                "title": item.title,
                                "date": item.scheduled_date,
                                "minutes": item.expected_minutes,
                                "priority": item.priority,
                            },
                        )
                    ).scalar_one_or_none()
                    if inserted is not None:
                        await connection.execute(
                            text(
                                "INSERT INTO notification_jobs(id,user_id,task_id,event_type,channel,"
                                "scheduled_at,payload,status,idempotency_key) VALUES(:id,:user_id,:task,"
                                "'TASK_REMINDER','IN_APP',:scheduled,CAST(:payload AS jsonb),'PENDING',:key) "
                                "ON CONFLICT(idempotency_key) DO NOTHING"
                            ),
                            {
                                "id": uuid4(),
                                "user_id": watermark["user_id"],
                                "task": task_id,
                                "scheduled": datetime.combine(
                                    item.scheduled_date,
                                    time(hour=8),
                                    tzinfo=ZoneInfo(timezone_name),
                                ).astimezone(UTC),
                                "payload": _json({"task_id": str(task_id), "title": item.title}),
                                "key": f"task-reminder:{task_id}",
                            },
                        )
                if unscheduled:
                    await connection.execute(
                        text(
                            "INSERT INTO plan_schedule_events(id,user_id,plan_id,plan_revision_id,"
                            "event_date,event_type,details) VALUES(:id,:user_id,:plan_id,:revision,"
                            ":date,'UNSCHEDULED',CAST(:details AS jsonb))"
                        ),
                        {
                            "id": uuid4(),
                            "user_id": watermark["user_id"],
                            "plan_id": watermark["plan_id"],
                            "revision": watermark["plan_revision_id"],
                            "date": cursor,
                            "details": _json({"items": unscheduled}),
                        },
                    )
                cursor += timedelta(days=1)
            await connection.execute(
                text(
                    "UPDATE plan_schedule_watermarks SET materialized_through=:through,updated_at=now() "
                    "WHERE id=:id"
                ),
                {"through": desired, "id": watermark["id"]},
            )
            return True
