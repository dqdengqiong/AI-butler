"""终态运行、事件、trace 与记忆审计的保留期清理。"""

from __future__ import annotations

from sqlalchemy import text

from .context import ButlerContext


class RetentionService:
    def __init__(self, context: ButlerContext) -> None:
        self.database = context.database
        self.settings = context.settings

    async def cleanup_once(self) -> bool:
        async with self.database.transaction() as connection:
            deleted = 0
            expired_workflows = await connection.execute(
                text(
                    "UPDATE workflow_sessions SET status='EXPIRED',completed_at=now(),updated_at=now(),"
                    "slots='{}'::jsonb WHERE status='WAITING_INPUT' AND "
                    "COALESCE(expires_at,updated_at+interval '7 days')<=now()"
                )
            )
            deleted += int(expired_workflows.rowcount or 0)
            for statement, days in (
                (
                    "DELETE FROM agent_run_events e USING agent_runs r WHERE e.agent_run_id=r.id "
                    "AND r.status IN ('SUCCEEDED','FAILED_FINAL','CANCELLED') "
                    "AND e.created_at<now()-(:days || ' days')::interval",
                    self.settings.event_retention_days,
                ),
                (
                    "DELETE FROM agent_trace_spans WHERE created_at<now()-(:days || ' days')::interval "
                    "AND agent_run_id IN (SELECT id FROM agent_runs WHERE status IN "
                    "('SUCCEEDED','FAILED_FINAL','CANCELLED'))",
                    self.settings.run_trace_retention_days,
                ),
                (
                    "DELETE FROM agent_runs WHERE updated_at<now()-(:days || ' days')::interval "
                    "AND status IN ('SUCCEEDED','FAILED_FINAL','CANCELLED')",
                    self.settings.run_trace_retention_days,
                ),
                (
                    "DELETE FROM memory_audit_records WHERE "
                    "created_at<now()-(:days || ' days')::interval",
                    self.settings.memory_audit_retention_days,
                ),
                (
                    "DELETE FROM conversations WHERE deleted_at IS NOT NULL "
                    "AND deleted_at<now()-(:days || ' days')::interval",
                    self.settings.deleted_conversation_retention_days,
                ),
            ):
                result = await connection.execute(text(statement), {"days": days})
                deleted += int(result.rowcount or 0)
            return deleted > 0
