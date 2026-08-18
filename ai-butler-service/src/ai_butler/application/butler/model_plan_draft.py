"""模型 Planner 草稿的规范化持久化服务。"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.agent.availability import AvailabilityInterpretationV1
from ai_butler.agent.contracts import PlannerResultV1, PlanScopeV1
from ai_butler.agent.evidence import NumberedEvidence
from ai_butler.agent.runtime import DEFAULT_CAPABILITY_REGISTRY

from .plan_draft import PlanDraftService
from .shared import _json
from .support import safe_summary


class ModelPlanDraftService(PlanDraftService):
    """只持久化已经通过确定性 Review 的模型计划。"""

    async def _create_model_plan_draft(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        content: str,
        evidence: tuple[NumberedEvidence, ...],
        availability: AvailabilityInterpretationV1,
        scope: PlanScopeV1,
        result: PlannerResultV1,
        *,
        intent: str,
    ) -> None:
        """在确定性 Review 通过后持久化模型草稿及规范化阶段/模板。"""

        DEFAULT_CAPABILITY_REGISTRY.require("plan_draft_write", "Planner", approved=False)
        plan = result.plan
        if result.status != "READY" or plan is None:
            raise ValueError("only reviewed READY plans can be persisted")
        if any(item.result.source_type == "KNOWLEDGE" for item in evidence):
            await self._ensure_synthetic_source(connection)

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
            (item for item in existing_plans if UUID(str(item["id"])) == target_plan_id), None
        )
        is_adjustment = intent == "PLAN_ADJUST" and bool(
            target_plan is not None or len(existing_plans) == 1
        )
        if intent == "PLAN_ADJUST" and len(existing_plans) > 1 and target_plan is None:
            await self._interrupt_for_plan_selection(connection, run, existing_plans, availability)
            return

        if is_adjustment:
            existing_plan = target_plan or existing_plans[0]
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
        else:
            goal_id, plan_id, revision_number, expected_revision = uuid4(), uuid4(), 1, None
            await connection.execute(
                text(
                    "INSERT INTO goals(id,user_id,goal_type,title,status,target_date) "
                    "VALUES(:id,:user_id,'CIVIL_SERVICE_EXAM',:title,'DRAFT',:target_date)"
                ),
                {
                    "id": goal_id,
                    "user_id": run["user_id"],
                    "title": plan.title,
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
                    "title": plan.title,
                },
            )

        revision_id, approval_id = uuid4(), uuid4()
        work_item_id = f"work-1-{revision_id}"
        revision_content = {
            "schema_version": "1.0",
            "planner_result": result.model_dump(mode="json"),
            "availability": availability.model_dump(mode="json"),
            "plan_scope": scope.model_dump(mode="json"),
            "work_item_id": work_item_id,
        }
        await connection.execute(
            text(
                "INSERT INTO plan_revisions(id,plan_id,user_id,agent_run_id,revision,status,"
                "objective_summary,start_date,end_date,weekly_minutes,change_reason,content) "
                "VALUES(:id,:plan_id,:user_id,:run_id,:revision,'PENDING_APPROVAL',:summary,"
                ":start,:end,:weekly_minutes,:reason,CAST(:content AS jsonb))"
            ),
            {
                "id": revision_id,
                "plan_id": plan_id,
                "user_id": run["user_id"],
                "run_id": run["id"],
                "revision": revision_number,
                "summary": plan.objective_summary,
                "start": plan.start_date,
                "end": plan.end_date,
                "weekly_minutes": plan.weekly_minutes,
                "reason": safe_summary(content),
                "content": _json(revision_content),
            },
        )
        for stage in sorted(plan.stages, key=lambda item: item.sequence):
            stage_id = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO plan_stages(id,plan_revision_id,sequence,title,objective,start_date,end_date) "
                    "VALUES(:id,:revision,:sequence,:title,:objective,:start,:end)"
                ),
                {
                    "id": stage_id,
                    "revision": revision_id,
                    "sequence": stage.sequence,
                    "title": stage.name,
                    "objective": stage.objective,
                    "start": stage.start_date,
                    "end": stage.end_date,
                },
            )
            for template_sequence, template in enumerate(stage.task_templates, 1):
                await connection.execute(
                    text(
                        "INSERT INTO plan_task_templates(id,plan_revision_id,stage_id,sequence,"
                        "template_key,title,expected_minutes,schedule_rule) "
                        "VALUES(:id,:revision,:stage,:sequence,:key,:title,:minutes,CAST(:rule AS jsonb))"
                    ),
                    {
                        "id": uuid4(),
                        "revision": revision_id,
                        "stage": stage_id,
                        "sequence": template_sequence,
                        "key": template.template_key,
                        "title": template.title,
                        "minutes": template.expected_minutes,
                        "rule": _json(
                            {
                                "stage_key": stage.stage_key,
                                "description": template.description,
                                "frequency": template.frequency,
                                "priority": template.priority,
                                "claim_keys": template.claim_keys,
                            }
                        ),
                    },
                )

        await connection.execute(
            text(
                "INSERT INTO approval_decisions(id,user_id,agent_run_id) VALUES(:id,:user_id,:run_id)"
            ),
            {"id": approval_id, "user_id": run["user_id"], "run_id": run["id"]},
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
        source_card = await self._persist_evidence(
            connection,
            run,
            evidence,
            claim_text="本计划只使用通过证据 Gate 的检索事实",
            plan_revision_id=revision_id,
        )
        card = {
            "schema_version": "1.1",
            "card_id": str(uuid4()),
            "card_type": "PlanCard",
            "entity_refs": {
                "approval_id": str(approval_id),
                "approval_version": 1,
                "items": [
                    {
                        "work_item_id": work_item_id,
                        "plan_id": str(plan_id),
                        "plan_revision_id": str(revision_id),
                        "expected_current_revision_id": (
                            str(expected_revision) if expected_revision else None
                        ),
                    }
                ],
            },
            "payload": {
                "mode": "SINGLE_PLAN_ADJUST" if is_adjustment else "SINGLE_PLAN_CREATE",
                "title": plan.title,
                "plans": [
                    {
                        "work_item_id": work_item_id,
                        "plan_id": str(plan_id),
                        "plan_revision_id": str(revision_id),
                        "title": plan.title,
                        "objective_summary": plan.objective_summary,
                        "weekly_minutes": plan.weekly_minutes,
                        "start_date": plan.start_date.isoformat(),
                        "end_date": plan.end_date.isoformat(),
                    }
                ],
                "total_weekly_minutes": plan.weekly_minutes,
                "available_weekly_minutes": int(availability.weekly_minutes or 0),
                "warnings": list(result.warnings),
            },
            "actions": [
                {"action_id": "approve", "action_type": "APPROVE", "label": "确认计划"},
                {"action_id": "edit", "action_type": "EDIT", "label": "继续修改"},
                {"action_id": "reject", "action_type": "REJECT", "label": "拒绝"},
            ],
        }
        response = "计划草稿已通过负荷、日期和引用校验。请确认、修改或拒绝。"
        cards = [card, *([source_card] if source_card else [])]
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "message.start",
            {"message_id": str(run["pending_response_message_id"])},
            run["attempt"],
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "message.delta",
            {"delta": response},
            run["attempt"],
        )
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
