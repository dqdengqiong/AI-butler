from __future__ import annotations

import asyncio
import logging

from ai_butler.application.butler import ButlerService
from ai_butler.config import get_settings
from ai_butler.infrastructure.database import AsyncDatabase
from ai_butler.workers.runtime import run_polling_worker

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    database = AsyncDatabase(settings.app_database_url)
    service = ButlerService(database, settings)

    async def poll_once() -> None:
        if not await service.scheduler_poll_once():
            logger.debug("scheduler_worker_idle")

    try:
        await run_polling_worker("scheduler", settings.worker_poll_interval_ms, poll_once)
    finally:
        await database.close()


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(run())


if __name__ == "__main__":
    main()
