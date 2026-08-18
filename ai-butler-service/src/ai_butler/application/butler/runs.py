from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text

from ai_butler.domain.errors import conflict, not_found
from ai_butler.security import issue_signed_ticket

from .context import ButlerContext
from .conversation_repository import ConversationRepository
from .events import EventService
from .shared import _row


class RunService:
    """管理单轮 run 的查询、取消和整轮重试。"""

    def __init__(
        self,
        context: ButlerContext,
        events: EventService,
        repository: ConversationRepository,
    ) -> None:
        self.database = context.database
        self.settings = context.settings
        self._append_event = events._append_event
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
        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "status": run["status"],
            "attempt": run["attempt"],
            "last_sequence": run["last_event_sequence"],
            "response_message": {"id": run["pending_response_message_id"]},
            "data": run["output_data"] if isinstance(run["output_data"], dict) else {},
            "citations": [],
            "warnings": run["warning_data"] if isinstance(run["warning_data"], list) else [],
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
                            "WHERE agent_run_id=:run_id AND user_id=:user_id AND sequence>:after "
                            "ORDER BY sequence LIMIT 100"
                        ),
                        {"run_id": run_id, "user_id": user_id, "after": after},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(item) for item in rows]

    async def event_owner(self, run_id: UUID) -> UUID:
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
                    "UPDATE agent_runs SET status=:status,cancel_requested_at=now(),updated_at=now() "
                    "WHERE id=:id"
                ),
                {"status": status, "id": run_id},
            )
            if status == "CANCELLED":
                await connection.execute(
                    text("UPDATE messages SET status='CANCELLED' WHERE id=:id"),
                    {"id": run["pending_response_message_id"]},
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
                    "UPDATE agent_runs SET status='QUEUED',attempt=attempt+1,error_code=NULL,"
                    "updated_at=now() WHERE id=:id AND user_id=:user_id "
                    "AND status='FAILED_RETRYABLE' AND attempt=:attempt RETURNING attempt"
                ),
                {"id": run_id, "user_id": user_id, "attempt": expected_attempt},
            )
            row = result.first()
            if row is None:
                raise conflict("RUN_RETRY_CONFLICT", "运行状态或尝试次数已更新")
            await self._append_event(connection, run_id, user_id, "message.reset", {}, row[0])
            return {"run_id": run_id, "status": "QUEUED", "attempt": row[0]}
