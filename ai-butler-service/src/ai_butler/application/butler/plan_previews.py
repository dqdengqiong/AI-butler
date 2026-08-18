from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import text

from ai_butler.api.schemas import PlanConfirmationResponseV1, PlanPreviewConfirmationRequestV1
from ai_butler.domain.errors import ButlerError, conflict, not_found

from .context import ButlerContext
from .shared import _content_hash, _json, _row


class PlanPreviewService:
    """在用户确认只读预览后，单事务写入全部计划业务事实。"""

    def __init__(self, context: ButlerContext) -> None:
        self.database = context.database

    async def confirm(
        self,
        user_id: UUID,
        message_id: UUID,
        request: PlanPreviewConfirmationRequestV1,
        idempotency_key: str,
    ) -> dict[str, object]:
        key_hash = _content_hash({"key": idempotency_key})
        request_hash = _content_hash(
            {"message_id": str(message_id), "request": request.model_dump(mode="json")}
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO request_idempotency_keys(id,user_id,scope,key_hash,request_hash) "
                    "VALUES(:id,:user_id,'PLAN_PREVIEW_CONFIRM',:key_hash,:request_hash) "
                    "ON CONFLICT(user_id,scope,key_hash) DO NOTHING"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "key_hash": key_hash,
                    "request_hash": request_hash,
                },
            )
            idempotency = _row(
                await connection.execute(
                    text(
                        "SELECT id,request_hash,response_data FROM request_idempotency_keys "
                        "WHERE user_id=:user_id AND scope='PLAN_PREVIEW_CONFIRM' "
                        "AND key_hash=:key_hash FOR UPDATE"
                    ),
                    {"user_id": user_id, "key_hash": key_hash},
                )
            )
            if idempotency is None:
                raise ButlerError("IDEMPOTENCY_RESERVATION_FAILED", "无法锁定确认请求", 500)
            if idempotency["request_hash"] != request_hash:
                raise conflict("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同的确认请求")
            if isinstance(idempotency["response_data"], dict):
                return PlanConfirmationResponseV1.model_validate(
                    idempotency["response_data"]
                ).model_dump(mode="json")
            timezone_name = str(
                (
                    await connection.execute(
                        text("SELECT timezone FROM users WHERE id=:id FOR UPDATE"),
                        {"id": user_id},
                    )
                ).scalar_one()
            )
            message = _row(
                await connection.execute(
                    text(
                        "SELECT m.*,r.id AS run_id FROM messages m JOIN agent_runs r "
                        "ON r.id=m.agent_run_id WHERE m.id=:id AND m.user_id=:user_id "
                        "AND m.role='ASSISTANT' FOR UPDATE OF m"
                    ),
                    {"id": message_id, "user_id": user_id},
                )
            )
            if message is None:
                raise not_found()
            structured = message["structured_content"]
            structured = structured if isinstance(structured, dict) else {}
            cards = structured.get("cards")
            cards = cards if isinstance(cards, list) else []
            card = next(
                (
                    item
                    for item in cards
                    if isinstance(item, dict) and item.get("card_type") == "PlanPreviewCard"
                ),
                None,
            )
            if card is None or not isinstance(card.get("payload"), dict):
                raise not_found()
            payload = card["payload"]
            if payload.get("preview_hash") != request.expected_preview_hash:
                raise conflict("PLAN_PREVIEW_HASH_MISMATCH", "计划预览已变化，请刷新后重试")
            if payload.get("status") == "CONFIRMED":
                refs = card.get("entity_refs")
                refs = refs if isinstance(refs, dict) else {}
                response = PlanConfirmationResponseV1(
                    preview_message_id=message_id,
                    plan_id=UUID(str(refs["plan_id"])),
                    revision_id=UUID(str(refs["revision_id"])),
                    task_ids=[UUID(str(value)) for value in refs.get("task_ids", [])],
                ).model_dump(mode="json")
                await connection.execute(
                    text(
                        "UPDATE request_idempotency_keys SET response_data=CAST(:response AS jsonb),"
                        "updated_at=now() WHERE id=:id"
                    ),
                    {"response": _json(response), "id": idempotency["id"]},
                )
                return response
            if payload.get("status") != "READY":
                raise conflict("PLAN_PREVIEW_NOT_CONFIRMABLE", "该计划预览已失效")
            expires_at = datetime.fromisoformat(str(payload["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                raise conflict("PLAN_PREVIEW_EXPIRED", "计划预览已过期，请重新生成")
            unsigned = {
                key: value
                for key, value in payload.items()
                if key not in {"status", "preview_hash"}
            }
            if _content_hash(unsigned) != request.expected_preview_hash:
                raise conflict("PLAN_PREVIEW_HASH_MISMATCH", "计划预览校验失败，请重新生成")
            plan_data = payload.get("plan")
            if not isinstance(plan_data, dict):
                raise ButlerError("PLAN_PREVIEW_INVALID", "计划预览内容无效", 422)
            stages = plan_data.get("stages")
            tasks = plan_data.get("tasks")
            if not isinstance(stages, list) or not stages or not isinstance(tasks, list):
                raise ButlerError("PLAN_PREVIEW_INVALID", "计划预览缺少阶段或任务", 422)
            start_date = datetime.fromisoformat(str(plan_data["start_date"])).date()
            end_date = datetime.fromisoformat(str(plan_data["end_date"])).date()
            today = datetime.now(ZoneInfo(timezone_name)).date()
            if start_date > end_date or start_date < today:
                raise conflict("PLAN_PREVIEW_DATE_CONFLICT", "计划日期已经失效，请重新生成")
            weekly_minutes = int(plan_data["weekly_minutes"])
            available_minutes = int(payload["available_weekly_minutes"])
            if weekly_minutes <= 0 or weekly_minutes > int(available_minutes * 0.85):
                raise ButlerError("PLAN_PREVIEW_LOAD_INVALID", "计划负荷超过可用时间", 422)
            task_minutes_by_week: dict[tuple[int, int], int] = {}
            for item in tasks:
                if not isinstance(item, dict):
                    continue
                scheduled_date = date.fromisoformat(str(item["scheduled_date"]))
                week = scheduled_date.isocalendar()[:2]
                task_minutes_by_week[week] = task_minutes_by_week.get(week, 0) + int(
                    item.get("expected_minutes", 0)
                )
            if any(minutes > weekly_minutes for minutes in task_minutes_by_week.values()):
                raise ButlerError("PLAN_PREVIEW_LOAD_INVALID", "自然周任务负荷超过计划容量", 422)

            operation = str(payload["operation"])
            revision_id = uuid4()
            if operation == "ADJUST":
                plan = _row(
                    await connection.execute(
                        text(
                            "SELECT * FROM plans WHERE id=:id AND user_id=:user_id "
                            "AND status='ACTIVE' FOR UPDATE"
                        ),
                        {"id": payload.get("target_plan_id"), "user_id": user_id},
                    )
                )
                if plan is None or str(plan["current_revision_id"]) != str(
                    payload.get("expected_current_revision_id")
                ):
                    raise conflict("PLAN_REVISION_CONFLICT", "计划已被更新，请重新生成预览")
                plan_id = UUID(str(plan["id"]))
                goal_id = UUID(str(plan["goal_id"]))
                revision_number = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT COALESCE(max(revision),0)+1 FROM plan_revisions "
                                "WHERE plan_id=:id"
                            ),
                            {"id": plan_id},
                        )
                    ).scalar_one()
                )
                await connection.execute(
                    text(
                        "UPDATE plan_revisions SET status='SUPERSEDED' WHERE id=:id "
                        "AND status='APPROVED'"
                    ),
                    {"id": plan["current_revision_id"]},
                )
                await connection.execute(
                    text(
                        "UPDATE tasks SET status='CANCELLED',cancellation_reason='PLAN_ADJUSTED',"
                        "updated_at=now() WHERE plan_id=:plan_id AND scheduled_date>=:today "
                        "AND status IN ('TODO','DOING')"
                    ),
                    {"plan_id": plan_id, "today": today},
                )
                await connection.execute(
                    text(
                        "UPDATE notification_jobs SET status='CANCELLED',updated_at=now() "
                        "WHERE task_id IN (SELECT id FROM tasks WHERE plan_id=:plan_id "
                        "AND cancellation_reason='PLAN_ADJUSTED') "
                        "AND status IN ('PENDING','RETRY','RUNNING')"
                    ),
                    {"plan_id": plan_id},
                )
                await connection.execute(
                    text(
                        "UPDATE goals SET title=:title,target_date=:target,updated_at=now() "
                        "WHERE id=:id"
                    ),
                    {"title": plan_data["objective_summary"], "target": end_date, "id": goal_id},
                )
            else:
                goal_id = uuid4()
                plan_id = uuid4()
                revision_number = 1
                await connection.execute(
                    text(
                        "INSERT INTO goals(id,user_id,goal_type,title,target_date,status) "
                        "VALUES(:id,:user_id,'CIVIL_SERVICE_EXAM',:title,:target,'ACTIVE')"
                    ),
                    {
                        "id": goal_id,
                        "user_id": user_id,
                        "title": plan_data["objective_summary"],
                        "target": end_date,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO plans(id,user_id,goal_id,title,status) "
                        "VALUES(:id,:user_id,:goal_id,:title,'ACTIVE')"
                    ),
                    {
                        "id": plan_id,
                        "user_id": user_id,
                        "goal_id": goal_id,
                        "title": plan_data["title"],
                    },
                )
            await connection.execute(
                text(
                    "INSERT INTO plan_revisions(id,plan_id,user_id,agent_run_id,revision,status,"
                    "objective_summary,start_date,end_date,weekly_minutes,change_reason,content,approved_at) "
                    "VALUES(:id,:plan_id,:user_id,:run_id,:revision,'APPROVED',:objective,:start,:end,"
                    ":weekly,:reason,CAST(:content AS jsonb),now())"
                ),
                {
                    "id": revision_id,
                    "plan_id": plan_id,
                    "user_id": user_id,
                    "run_id": message["run_id"],
                    "revision": revision_number,
                    "objective": plan_data["objective_summary"],
                    "start": start_date,
                    "end": end_date,
                    "weekly": weekly_minutes,
                    "reason": "用户确认计划预览",
                    "content": _json(
                        {
                            "plan": plan_data,
                            "availability": payload["availability"],
                            "scenario_code": payload["scenario_code"],
                            "scenario_fields": payload["scenario_fields"],
                        }
                    ),
                },
            )
            await connection.execute(
                text(
                    "UPDATE plans SET title=:title,current_revision_id=:revision,updated_at=now() "
                    "WHERE id=:id"
                ),
                {"title": plan_data["title"], "revision": revision_id, "id": plan_id},
            )
            for stage in stages:
                if not isinstance(stage, dict):
                    raise ButlerError("PLAN_PREVIEW_INVALID", "计划阶段无效", 422)
                stage_id = uuid4()
                stage_key = str(stage["stage_key"])
                await connection.execute(
                    text(
                        "INSERT INTO plan_stages(id,plan_revision_id,stage_key,sequence,title,objective,"
                        "start_date,end_date) VALUES(:id,:revision,:key,:sequence,:title,:objective,:start,:end)"
                    ),
                    {
                        "id": stage_id,
                        "revision": revision_id,
                        "key": stage_key,
                        "sequence": int(stage["sequence"]),
                        "title": stage["name"],
                        "objective": stage["objective"],
                        "start": stage["start_date"],
                        "end": stage["end_date"],
                    },
                )
                for sequence, template in enumerate(stage.get("task_templates", []), 1):
                    await connection.execute(
                        text(
                            "INSERT INTO plan_task_templates(id,plan_revision_id,stage_id,sequence,"
                            "template_key,title,expected_minutes,schedule_rule) VALUES(:id,:revision,"
                            ":stage,:sequence,:key,:title,:minutes,CAST(:rule AS jsonb))"
                        ),
                        {
                            "id": uuid4(),
                            "revision": revision_id,
                            "stage": stage_id,
                            "sequence": sequence,
                            "key": template["template_key"],
                            "title": template["title"],
                            "minutes": int(template["expected_minutes"]),
                            "rule": _json(
                                {
                                    "stage_key": stage_key,
                                    "frequency": template.get("frequency", {}),
                                    "priority": template.get("priority", 3),
                                }
                            ),
                        },
                    )
            task_ids: list[UUID] = []
            for item in tasks:
                if not isinstance(item, dict):
                    raise ButlerError("PLAN_PREVIEW_INVALID", "计划任务无效", 422)
                scheduled_date = datetime.fromisoformat(str(item["scheduled_date"])).date()
                if scheduled_date < today or scheduled_date > today + timedelta(days=6):
                    raise ButlerError("PLAN_PREVIEW_DATE_CONFLICT", "任务日期超出七日预览", 422)
                task_id = uuid5(revision_id, str(item["task_key"]))
                task_ids.append(task_id)
                await connection.execute(
                    text(
                        "INSERT INTO tasks(id,user_id,plan_id,plan_revision_id,task_key,title,"
                        "scheduled_date,expected_minutes,priority,status) VALUES(:id,:user_id,:plan_id,"
                        ":revision,:key,:title,:date,:minutes,:priority,'TODO')"
                    ),
                    {
                        "id": task_id,
                        "user_id": user_id,
                        "plan_id": plan_id,
                        "revision": revision_id,
                        "key": item["task_key"],
                        "title": item["title"],
                        "date": scheduled_date,
                        "minutes": int(item["expected_minutes"]),
                        "priority": int(item["priority"]),
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO notification_jobs(id,user_id,task_id,event_type,channel,"
                        "scheduled_at,payload,status,idempotency_key) VALUES(:id,:user_id,:task_id,"
                        "'TASK_REMINDER','IN_APP',:scheduled,CAST(:payload AS jsonb),'PENDING',:key)"
                    ),
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "task_id": task_id,
                        "scheduled": datetime.combine(
                            scheduled_date, time(hour=8), tzinfo=ZoneInfo(timezone_name)
                        ).astimezone(UTC),
                        "payload": _json({"task_id": str(task_id), "title": item["title"]}),
                        "key": f"task-reminder:{task_id}",
                    },
                )
            materialized_through = min(end_date, today + timedelta(days=6))
            await connection.execute(
                text(
                    "INSERT INTO plan_schedule_watermarks(id,user_id,plan_id,plan_revision_id,"
                    "materialized_through) VALUES(:id,:user_id,:plan_id,:revision,:through) "
                    "ON CONFLICT(plan_id) DO UPDATE SET plan_revision_id=EXCLUDED.plan_revision_id,"
                    "materialized_through=EXCLUDED.materialized_through,updated_at=now()"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "plan_id": plan_id,
                    "revision": revision_id,
                    "through": materialized_through,
                },
            )
            for index, source in enumerate(payload.get("evidence", []), 1):
                if not isinstance(source, dict):
                    continue
                claim_id = uuid4()
                await connection.execute(
                    text(
                        "INSERT INTO claims(id,agent_run_id,plan_revision_id,claim_key,claim_text,"
                        "claim_type) VALUES(:id,:run,:revision,:key,:text,'FACT')"
                    ),
                    {
                        "id": claim_id,
                        "run": message["run_id"],
                        "revision": revision_id,
                        "key": f"plan-source-{index}",
                        "text": str(source.get("excerpt") or source.get("title") or "来源")[:4000],
                    },
                )
                source_type = str(source.get("source_type") or "WEB")
                if source_type == "WEB" and not source.get("source_url"):
                    continue
                await connection.execute(
                    text(
                        "INSERT INTO citations(id,claim_id,source_type,source_url_snapshot,"
                        "source_title_snapshot,source_domain_snapshot,published_at_snapshot,"
                        "retrieved_at_snapshot,evidence_excerpt,relation,relevance_score,source_rank) "
                        "VALUES(:id,:claim,:type,:url,:title,:domain,:published,now(),:excerpt,"
                        "'SUPPORTS',1,:rank)"
                    ),
                    {
                        "id": uuid4(),
                        "claim": claim_id,
                        "type": source_type,
                        "url": source.get("source_url"),
                        "title": str(source.get("title") or "来源")[:300],
                        "domain": source.get("domain"),
                        "published": source.get("published_at"),
                        "excerpt": str(source.get("excerpt") or "")[:1000],
                        "rank": index,
                    },
                )
            payload["status"] = "CONFIRMED"
            card["entity_refs"] = {
                "goal_id": str(goal_id),
                "plan_id": str(plan_id),
                "revision_id": str(revision_id),
                "task_ids": [str(value) for value in task_ids],
            }
            await connection.execute(
                text(
                    "UPDATE messages SET structured_content=CAST(:structured AS jsonb),updated_at=now() "
                    "WHERE id=:id"
                ),
                {"structured": _json(structured), "id": message_id},
            )
            response = PlanConfirmationResponseV1(
                preview_message_id=message_id,
                plan_id=plan_id,
                revision_id=revision_id,
                task_ids=task_ids,
            ).model_dump(mode="json")
            await connection.execute(
                text(
                    "UPDATE request_idempotency_keys SET response_data=CAST(:response AS jsonb),"
                    "updated_at=now() WHERE id=:id"
                ),
                {"response": _json(response), "id": idempotency["id"]},
            )
            return response
