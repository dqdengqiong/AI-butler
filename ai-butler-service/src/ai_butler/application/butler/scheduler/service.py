"""PostgreSQL 作业队列 Scheduler 组合入口。"""

from __future__ import annotations

from ..context import ButlerContext
from ..memory import LongTermMemoryService
from ..memory.jobs import SchedulerMemoryJobsMixin
from ..retention import RetentionService
from .operations import SchedulerOperationsMixin
from .rolling import RollingScheduleMixin


class SchedulerService(
    SchedulerMemoryJobsMixin,
    SchedulerOperationsMixin,
    RollingScheduleMixin,
):
    def __init__(self, context: ButlerContext) -> None:
        self.database = context.database
        self.settings = context.settings
        self.embedding_provider = context.embedding_provider
        self.vector_store = context.vector_store
        self.notification_provider = context.notification_provider
        self.memory = LongTermMemoryService(context)
        self.retention = RetentionService(context)

    async def scheduler_poll_once(self) -> bool:
        """按副作用优先级处理一个可安全重试的作业。"""

        if await self._materialize_one_plan_window():
            return True
        if await self._delete_one_knowledge_vector():
            return True
        if await self._ingest_one_private_file():
            return True
        if await self._extract_one_memory():
            return True
        if await self._refresh_one_profile_snapshot():
            return True
        if await self._cleanup_one_memory_record():
            return True
        if await self._cleanup_one_store_orphan():
            return True
        if await self._send_one_notification():
            return True
        if await self._run_one_account_deletion_step():
            return True
        if await self._delete_one_expired_checkpoint():
            return True
        return await self.retention.cleanup_once()
