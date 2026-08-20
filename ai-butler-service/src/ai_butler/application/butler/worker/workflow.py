"""Worker 执行期间的跨轮 Workflow 状态操作。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import text

from ..shared import _json, _row
from .context import RunContext


class WorkflowSessionMixin:
    database: Any

    async def _active_plan_workflow(self, context: RunContext) -> dict[str, Any] | None:
        async with self.database.connect() as connection:
            return _row(
                await connection.execute(
                    text(
                        "SELECT id,slots,version FROM workflow_sessions WHERE user_id=:user_id "
                        "AND conversation_id=:conversation_id AND segment_id=:segment_id "
                        "AND workflow_type='PLAN_REQUIREMENTS' "
                        "AND status IN ('ACTIVE','WAITING_INPUT') ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {
                        "user_id": context.run["user_id"],
                        "conversation_id": context.run["conversation_id"],
                        "segment_id": context.run["segment_id"],
                    },
                )
            )

    async def _save_plan_workflow(
        self,
        context: RunContext,
        workflow: dict[str, Any] | None,
        slots: object,
    ) -> None:
        async with self.database.transaction() as connection:
            if workflow is None:
                await connection.execute(
                    text(
                        "INSERT INTO workflow_sessions(id,user_id,conversation_id,segment_id,"
                        "workflow_type,status,slots,expires_at) VALUES(:id,:user_id,:conversation_id,"
                        ":segment_id,'PLAN_REQUIREMENTS','WAITING_INPUT',CAST(:slots AS jsonb),"
                        "now()+interval '7 days')"
                    ),
                    {
                        "id": uuid4(),
                        "user_id": context.run["user_id"],
                        "conversation_id": context.run["conversation_id"],
                        "segment_id": context.run["segment_id"],
                        "slots": _json(slots),
                    },
                )
            else:
                await connection.execute(
                    text(
                        "UPDATE workflow_sessions SET status='WAITING_INPUT',slots=CAST(:slots AS jsonb),"
                        "version=version+1,expires_at=now()+interval '7 days',updated_at=now() "
                        "WHERE id=:id"
                    ),
                    {"id": workflow["id"], "slots": _json(slots)},
                )

    async def _complete_plan_workflow(self, workflow: dict[str, Any] | None) -> None:
        if workflow is None:
            return
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE workflow_sessions SET status='COMPLETED',slots='{}'::jsonb,"
                    "completed_at=now(),updated_at=now() WHERE id=:id"
                ),
                {"id": workflow["id"]},
            )
