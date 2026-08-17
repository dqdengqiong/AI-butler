from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.agent.availability import (
    AvailabilityInterpretationV1,
)
from ai_butler.agent.evidence import NumberedEvidence
from ai_butler.agent.runtime import DEFAULT_CAPABILITY_REGISTRY
from ai_butler.domain.errors import conflict

from .bootstrap import BootstrapService
from .events import EventService
from .evidence_execution import EvidenceExecutionService
from .shared import (
    _json,
    _row,
)
from .support import draft_tasks_for_availability, safe_summary


class PlanExecutionService:
    def __init__(
        self,
        events: EventService,
        evidence: EvidenceExecutionService,
        bootstrap: BootstrapService,
    ) -> None:
        self._append_event = events._append_event
        self._persist_evidence = evidence._persist_evidence
        self._ensure_synthetic_source = bootstrap._ensure_synthetic_source
        self._safe_summary = safe_summary
        self._draft_tasks_for_availability = draft_tasks_for_availability

    async def _create_plan_draft(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        content: str,
        evidence: tuple[NumberedEvidence, ...],
        availability: AvailabilityInterpretationV1,
    ) -> None:
        DEFAULT_CAPABILITY_REGISTRY.require("plan_draft_write", "Planner", approved=False)
        if any(item.result.source_type == "KNOWLEDGE" for item in evidence):
            await self._ensure_synthetic_source(connection)
        existing_plan = _row(
            await connection.execute(
                text(
                    "SELECT * FROM plans WHERE user_id=:user_id AND status='ACTIVE' ORDER BY created_at LIMIT 1"
                ),
                {"user_id": run["user_id"]},
            )
        )
        if existing_plan:
            plan_id = existing_plan["id"]
            goal_id = existing_plan["goal_id"]
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
            expected_revision = existing_plan["current_revision_id"]
            mode = "SINGLE_PLAN_ADJUST"
        else:
            goal_id, plan_id, revision_number, expected_revision = uuid4(), uuid4(), 1, None
            mode = "CREATE"
            await connection.execute(
                text(
                    "INSERT INTO goals(id,user_id,goal_type,title,status) "
                    "VALUES(:id,:user_id,'CIVIL_SERVICE_EXAM','公务员考试备考','DRAFT')"
                ),
                {"id": goal_id, "user_id": run["user_id"]},
            )
            await connection.execute(
                text(
                    "INSERT INTO plans(id,user_id,goal_id,title,status) "
                    "VALUES(:id,:user_id,:goal_id,'公务员备考','DRAFT')"
                ),
                {"id": plan_id, "user_id": run["user_id"], "goal_id": goal_id},
            )
        revision_id = uuid4()
        start = datetime.now(UTC).date()
        end = start + timedelta(days=27)
        weekly_minutes = int(availability.weekly_minutes or 0)
        tasks = self._draft_tasks_for_availability(start, availability)
        summary = f"四周基础验证计划：{availability.summary}；安排行测、申论与每周复盘"
        await connection.execute(
            text(
                "INSERT INTO plan_revisions(id,plan_id,user_id,agent_run_id,revision,status,objective_summary,"
                "start_date,end_date,weekly_minutes,change_reason,content) "
                "VALUES(:id,:plan_id,:user_id,:run_id,:revision,'DRAFT',:summary,:start,:end,"
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
                "reason": self._safe_summary(content),
                "content": _json(
                    {
                        "tasks": tasks,
                        "availability": availability.model_dump(mode="json"),
                    }
                ),
            },
        )
        approval_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO approval_decisions(id,user_id,agent_run_id) VALUES(:id,:user_id,:run_id)"
            ),
            {"id": approval_id, "user_id": run["user_id"], "run_id": run["id"]},
        )
        await connection.execute(
            text(
                "INSERT INTO approval_decision_items(id,approval_id,plan_id,plan_revision_id,expected_current_revision_id) "
                "VALUES(:id,:approval,:plan,:revision,:expected)"
            ),
            {
                "id": uuid4(),
                "approval": approval_id,
                "plan": plan_id,
                "revision": revision_id,
                "expected": expected_revision,
            },
        )
        source_card = await self._persist_evidence(
            connection,
            run,
            evidence,
            claim_text="本计划参考了检索结果中的公务员备考科目与训练建议",
            plan_revision_id=revision_id,
        )
        card = {
            "schema_version": "1.0",
            "card_id": str(uuid4()),
            "card_type": "PlanCard",
            "entity_refs": {
                "approval_id": str(approval_id),
                "approval_version": 1,
                "items": [
                    {
                        "plan_id": str(plan_id),
                        "plan_revision_id": str(revision_id),
                        "expected_current_revision_id": str(expected_revision)
                        if expected_revision
                        else None,
                    }
                ],
            },
            "payload": {
                "mode": mode,
                "title": "公务员备考 · 四周验证计划",
                "objective_summary": summary,
                "weekly_minutes": weekly_minutes,
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
                "UPDATE messages SET status='COMPLETED',content=:content,structured_content=CAST(:cards AS jsonb),"
                "updated_at=now() WHERE id=:id"
            ),
            {
                "content": response,
                "cards": _json({"cards": cards}),
                "id": run["response_message_id"],
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
            {"message_id": str(run["response_message_id"]), "content": response, "cards": cards},
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
        reduced_weekly_minutes: list[int] = []
        for item in items:
            current_weekly_minutes = int(
                (
                    await connection.execute(
                        text("SELECT weekly_minutes FROM plan_revisions WHERE id=:id"),
                        {"id": item["plan_revision_id"]},
                    )
                ).scalar_one()
            )
            # “降低负荷”按当前草案递减，绝不能回到历史硬编码值而突破用户刚确认的上限。
            next_weekly_minutes = max(30, current_weekly_minutes * 5 // 6)
            reduced_weekly_minutes.append(next_weekly_minutes)
            await connection.execute(
                text(
                    "UPDATE plan_revisions SET objective_summary=:summary,weekly_minutes=:weekly_minutes,"
                    "change_reason=:reason WHERE id=:id"
                ),
                {
                    "summary": "已按反馈降低负荷的四周公务员备考计划",
                    "weekly_minutes": next_weekly_minutes,
                    "reason": approval["feedback"],
                    "id": item["plan_revision_id"],
                },
            )
        new_version = approval["approval_version"] + 1
        await connection.execute(
            text(
                "UPDATE approval_decisions SET status='PENDING',action=NULL,decided_at=NULL,"
                "approval_version=:version WHERE id=:id"
            ),
            {"version": new_version, "id": approval["id"]},
        )
        response = "我已根据反馈更新草案，请再次使用计划卡确认。"
        message = _row(
            await connection.execute(
                text("SELECT structured_content FROM messages WHERE id=:id FOR UPDATE"),
                {"id": run["response_message_id"]},
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
            refs["approval_version"] = new_version
            refs["approval_status"] = "PENDING"
            payload = card.get("payload")
            if isinstance(payload, dict):
                payload["objective_summary"] = "已按反馈降低负荷的四周公务员备考计划"
                payload["weekly_minutes"] = min(reduced_weekly_minutes)
        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,"
                "structured_content=CAST(:cards AS jsonb),updated_at=now() WHERE id=:id"
            ),
            {
                "content": response,
                "cards": _json(structured if isinstance(structured, dict) else {}),
                "id": run["response_message_id"],
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
            {
                "type": "APPROVAL",
                "approval_id": str(approval["id"]),
                "approval_version": new_version,
            },
            run["attempt"],
        )

    async def _publish_revision(
        self, connection: AsyncConnection, user_id: UUID, item: dict[str, Any]
    ) -> None:
        DEFAULT_CAPABILITY_REGISTRY.require("plan_publish", "Executor", approved=True)
        DEFAULT_CAPABILITY_REGISTRY.require("task_materialize", "Executor", approved=True)
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
                "UPDATE plans SET current_revision_id=:revision,status='ACTIVE',updated_at=now() WHERE id=:plan;"
            ),
            {"revision": revision["id"], "plan": item["plan_id"]},
        )
        await connection.execute(
            text(
                "UPDATE goals SET status='ACTIVE',updated_at=now() WHERE id=(SELECT goal_id FROM plans WHERE id=:plan)"
            ),
            {"plan": item["plan_id"]},
        )
        tasks = (revision["content"] or {}).get("tasks", [])
        for index, task in enumerate(tasks):
            task_id = uuid5(UUID(str(revision["id"])), f"task:{index}")
            scheduled = revision["start_date"] + timedelta(days=int(task["day_offset"]))
            await connection.execute(
                text(
                    "INSERT INTO tasks(id,user_id,plan_id,plan_revision_id,title,scheduled_date,expected_minutes) "
                    "VALUES(:id,:user_id,:plan,:revision,:title,:date,:minutes) ON CONFLICT(id) DO NOTHING"
                ),
                {
                    "id": task_id,
                    "user_id": user_id,
                    "plan": item["plan_id"],
                    "revision": revision["id"],
                    "title": task["title"],
                    "date": scheduled,
                    "minutes": task["minutes"],
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO notification_jobs(id,user_id,task_id,event_type,channel,scheduled_at,status,idempotency_key) "
                    "VALUES(:id,:user_id,:task,'TASK_REMINDER','IN_APP',:scheduled,'PENDING',:key) "
                    "ON CONFLICT(idempotency_key) DO NOTHING"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "task": task_id,
                    "scheduled": datetime.combine(scheduled, datetime.min.time(), UTC),
                    "key": f"task-reminder:{task_id}:in-app",
                },
            )
