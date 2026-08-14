from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def run_polling_worker(
    name: str,
    poll_interval_ms: int,
    poll_once: Callable[[], Awaitable[None]],
) -> None:
    logger.info("worker_started name=%s", name)
    while True:
        await poll_once()
        await asyncio.sleep(poll_interval_ms / 1000)
