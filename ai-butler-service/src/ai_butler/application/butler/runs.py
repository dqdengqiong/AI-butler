from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text

from ai_butler.api.schemas import (
    ApprovalDecisionRequest,
)
from ai_butler.domain.errors import ButlerError, conflict, not_found
from ai_butler.security import issue_signed_ticket

from .context import ButlerContext
from .conversation_repository import ConversationRepository
from .events import EventService
from .plan_execution import PlanExecutionService
from .shared import (
    _row,
)


class RunService:
    def __init__(
        self,
        context: ButlerContext,
        events: EventService,
        planning: PlanExecutionService,
        repository: ConversationRepository,
    ) -> None:
        self.database = context.database
        self.settings = context.settings
        self._append_event = events._append_event
        self._publish_revision = planning._publish_revision
        self._reserve_execution_slot = repository._reserve_execution_slot

    async def get_run(self, user_id: UUID, run_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id AND user_id=:user_id"),
                    {"id": run_id, "user_id": user_id},
                )
            )
            if run is None:
                raise not_found()
            approval = _row(
                await connection.execute(
                    text(
                        "SELECT id,approval_version FROM approval_decisions "
                        "WHERE agent_run_id=:run_id AND status='PENDING' ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"run_id": run_id},
                )
            )
            return {
                "schema_version": "2.0",
                "run_id": run_id,
                "status": run["status"],
                "attempt": run["attempt"],
                "last_sequence": run["last_event_sequence"],
                "response_message": {"id": run["response_message_id"]},
                "data": {},
                "citations": [],
                "warnings": [],
                "next_action": (
                    {
                        "type": "REVIEW_PLAN",
                        "approval_id": approval["id"],
                        "approval_version": approval["approval_version"],
                    }
                    if approval
                    else None
                ),
                "error": (
                    {"code": run["error_code"], "retryable": run["status"] == "FAILED_RETRYABLE"}
                    if run["error_code"]
                    else None
                ),
                "created_at": run["created_at"],
                "updated_at": run["updated_at"],
            }

    async def stream_ticket(self, user_id: UUID, run_id: UUID) -> dict[str, object]:
        run = await self.get_run(user_id, run_id)
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.stream_ticket_seconds)
        return {
            "events_url": f"/v1/agent-runs/{run_id}/events",
            "ticket": issue_signed_ticket(
                run_id, self.settings.stream_ticket_secret, self.settings.stream_ticket_seconds
            ),
            "expires_at": expires_at,
            "last_sequence": run["last_sequence"],
        }

    async def list_events(self, user_id: UUID, run_id: UUID, after: int) -> list[dict[str, object]]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT event_type,sequence,attempt,payload,created_at FROM agent_run_events "
                            "WHERE run_id=:run_id AND user_id=:user_id AND sequence>:after ORDER BY sequence LIMIT 100"
                        ),
                        {"run_id": run_id, "user_id": user_id, "after": after},
                    )
                )
                .mappings()
                .all()
            )
            return [dict(item) for item in rows]

    async def event_owner(self, run_id: UUID) -> UUID:
        """返回流票据对应 run 的所有者；仅在票据验证后用于隔离查询。"""

        async with self.database.connect() as connection:
            value = (
                await connection.execute(
                    text("SELECT user_id FROM agent_runs WHERE id=:id"), {"id": run_id}
                )
            ).scalar_one_or_none()
            if value is None:
                raise not_found()
            return UUID(str(value))

    async def cancel_run(self, user_id: UUID, run_id: UUID) -> dict[str, object]:
        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id AND user_id=:user_id FOR UPDATE"),
                    {"id": run_id, "user_id": user_id},
                )
            )
            if run is None:
                raise not_found()
            if run["status"] in {"SUCCEEDED", "FAILED_FINAL", "CANCELLED"}:
                return {"run_id": run_id, "status": run["status"]}
            status = "CANCEL_REQUESTED" if run["status"] == "RUNNING" else "CANCELLED"
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status=:status,cancel_requested_at=now(),updated_at=now() WHERE id=:id"
                ),
                {"status": status, "id": run_id},
            )
            if status == "CANCELLED":
                await connection.execute(
                    text("UPDATE messages SET status='CANCELLED' WHERE id=:id"),
                    {"id": run["response_message_id"]},
                )
                await self._append_event(
                    connection, run_id, user_id, "run.cancelled", {}, run["attempt"]
                )
            return {"run_id": run_id, "status": status}

    async def retry_run(
        self,
        user_id: UUID,
        run_id: UUID,
        expected_attempt: int,
        execution_policy: str = "REJECT",
    ) -> dict[str, object]:
        async with self.database.transaction() as connection:
            owner = _row(
                await connection.execute(
                    text("SELECT id FROM users WHERE id=:user_id FOR UPDATE"),
                    {"user_id": user_id},
                )
            )
            if owner is None:
                raise not_found()
            run = _row(
                await connection.execute(
                    text(
                        "SELECT id,status,attempt FROM agent_runs WHERE id=:id "
                        "AND user_id=:user_id FOR UPDATE"
                    ),
                    {"id": run_id, "user_id": user_id},
                )
            )
            if (
                run is None
                or run["status"] != "FAILED_RETRYABLE"
                or int(run["attempt"]) != expected_attempt
            ):
                raise conflict("RUN_RETRY_CONFLICT", "运行状态或尝试次数已更新")
            await self._reserve_execution_slot(connection, user_id, run_id, execution_policy)
            result = await connection.execute(
                text(
                    "UPDATE agent_runs SET status='QUEUED',pending_action='RETRY',attempt=attempt+1,"
                    "pending_action_key=:key,error_code=NULL,updated_at=now() "
                    "WHERE id=:id AND user_id=:user_id AND status='FAILED_RETRYABLE' AND attempt=:attempt RETURNING attempt"
                ),
                {
                    "id": run_id,
                    "user_id": user_id,
                    "attempt": expected_attempt,
                    "key": f"retry:{run_id}:{expected_attempt + 1}",
                },
            )
            row = result.first()
            if row is None:
                raise conflict("RUN_RETRY_CONFLICT", "运行状态或尝试次数已更新")
            await self._append_event(connection, run_id, user_id, "message.reset", {}, row[0])
            return {"run_id": run_id, "status": "QUEUED", "attempt": row[0]}

    async def decide_approval(
        self, user_id: UUID, approval_id: UUID, request: ApprovalDecisionRequest
    ) -> dict[str, object]:
        if approval_id != request.approval_id:
            raise ButlerError("APPROVAL_ID_MISMATCH", "审批标识不匹配", 400)
        async with self.database.transaction() as connection:
            owner = _row(
                await connection.execute(
                    text("SELECT id FROM users WHERE id=:user_id FOR UPDATE"),
                    {"user_id": user_id},
                )
            )
            if owner is None:
                raise not_found()
            approval = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM approval_decisions WHERE id=:id AND user_id=:user_id FOR UPDATE"
                    ),
                    {"id": approval_id, "user_id": user_id},
                )
            )
            if approval is None:
                raise not_found()
            if approval["status"] != "PENDING":
                return {"approval_id": approval_id, "status": approval["status"]}
            if approval["approval_version"] != request.expected_approval_version:
                raise ButlerError(
                    "APPROVAL_VERSION_CONFLICT",
                    "审批版本已更新，请刷新后重试",
                    409,
                    details={
                        "approval_id": str(approval_id),
                        "current_approval_version": int(approval["approval_version"]),
                    },
                )
            run_id = UUID(str(approval["agent_run_id"]))
            await self._reserve_execution_slot(
                connection, user_id, run_id, request.execution_policy
            )
            items = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM approval_decision_items WHERE approval_id=:id FOR UPDATE"
                        ),
                        {"id": approval_id},
                    )
                )
                .mappings()
                .all()
            )
            if request.action == "APPROVE":
                for item in items:
                    plan = _row(
                        await connection.execute(
                            text("SELECT current_revision_id FROM plans WHERE id=:id FOR UPDATE"),
                            {"id": item["plan_id"]},
                        )
                    )
                    expected = item["expected_current_revision_id"]
                    if plan is None or plan["current_revision_id"] != expected:
                        raise conflict("PLAN_REVISION_CONFLICT", "计划版本已更新，请重新生成草案")
                for item in items:
                    await self._publish_revision(connection, user_id, dict(item))
            terminal = "APPROVED" if request.action == "APPROVE" else request.action + "ED"
            await connection.execute(
                text(
                    "UPDATE approval_decisions SET status=:status,action=:action,feedback=:feedback,decided_at=now() "
                    "WHERE id=:id"
                ),
                {
                    "status": terminal,
                    "action": request.action,
                    "feedback": request.feedback,
                    "id": approval_id,
                },
            )
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='QUEUED',pending_action='APPROVAL_RESUME',"
                    "pending_action_key=:key,updated_at=now() WHERE id=:run_id"
                ),
                {
                    "key": f"approval:{approval_id}:{request.expected_approval_version}",
                    "run_id": run_id,
                },
            )
            await self._append_event(
                connection,
                run_id,
                user_id,
                "run.status",
                {"status": "QUEUED", "approval_action": request.action},
                0,
            )
            return {"approval_id": approval_id, "status": terminal, "run_id": run_id}
