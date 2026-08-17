from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from ai_butler.domain.errors import ButlerError

from .completion import CompletionService
from .context import ButlerContext
from .events import EventService
from .executor import RunExecutor
from .shared import (
    _row,
)


class WorkerService:
    def __init__(
        self,
        context: ButlerContext,
        events: EventService,
        executor: RunExecutor,
        completion: CompletionService,
    ) -> None:
        self.database = context.database
        self.settings = context.settings
        self._append_event = events._append_event
        self._execute_run = executor._execute_run
        self._fail_run = completion._fail_run

    async def worker_poll_once(self, worker_id: UUID) -> bool:
        """领取并执行一个 run；网络/模型实现接入时应在领取事务提交后调用。"""

        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM agent_runs WHERE status='QUEUED' OR "
                        "(status IN ('RUNNING','CANCEL_REQUESTED') AND lease_expires_at<now()) "
                        "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
                    )
                )
            )
            if run is None:
                return False
            if run["status"] == "CANCEL_REQUESTED":
                await connection.execute(
                    text("UPDATE agent_runs SET status='CANCELLED',updated_at=now() WHERE id=:id"),
                    {"id": run["id"]},
                )
                await self._append_event(
                    connection, run["id"], run["user_id"], "run.cancelled", {}, run["attempt"]
                )
                return True
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='RUNNING',worker_id=:worker,heartbeat_at=now(),"
                    "lease_expires_at=now()+(:lease || ' seconds')::interval,updated_at=now() WHERE id=:id"
                ),
                {"worker": worker_id, "lease": self.settings.worker_lease_seconds, "id": run["id"]},
            )
            await self._append_event(
                connection,
                run["id"],
                run["user_id"],
                "run.status",
                {"status": "RUNNING"},
                run["attempt"],
            )
        try:
            await self._execute_run(UUID(str(run["id"])))
        except ButlerError as exc:
            await self._fail_run(UUID(str(run["id"])), exc)
        except Exception:
            await self._fail_run(
                UUID(str(run["id"])),
                ButlerError("AGENT_INTERNAL_ERROR", "管家暂时无法完成处理", 500, True),
            )
        return True
