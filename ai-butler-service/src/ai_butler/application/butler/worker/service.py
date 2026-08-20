"""Agent Worker 的运行领取与执行入口。"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text

from ai_butler.domain.errors import ButlerError

from ..bootstrap import BootstrapService
from ..context import ButlerContext
from ..events import EventService
from ..shared import _row
from .completion import CompletionService
from .evidence import EvidenceExecutionService
from .executor import RunExecutor
from .graph import ButlerGraphRuntime

logger = logging.getLogger(__name__)


class WorkerService:
    def __init__(
        self,
        context: ButlerContext,
        events: EventService,
        bootstrap: BootstrapService,
    ) -> None:
        completion = CompletionService(context, events)
        evidence = EvidenceExecutionService(context, events, completion, bootstrap)
        executor = RunExecutor(context, events, evidence, completion)
        self.database = context.database
        self.settings = context.settings
        self._append_event = events._append_event
        self._graph = ButlerGraphRuntime(context, executor)
        self._fail_run_handler = completion._fail_run

    async def fail_run(self, run_id: UUID, error: ButlerError) -> None:
        await self._fail_run_handler(run_id, error)

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
            await self._graph.run(UUID(str(run["id"])))
        except ButlerError as exc:
            await self._fail_run_handler(UUID(str(run["id"])), exc)
        except Exception:
            logger.exception("agent run failed", extra={"run_id": str(run["id"])})
            await self._fail_run_handler(
                UUID(str(run["id"])),
                ButlerError("AGENT_INTERNAL_ERROR", "管家暂时无法完成处理", 500, True),
            )
        return True
