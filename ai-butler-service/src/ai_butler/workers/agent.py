from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import subprocess
from uuid import uuid4

from ai_butler.adapters.model_routing import load_model_routing
from ai_butler.application.butler import ButlerService
from ai_butler.config import get_settings
from ai_butler.infrastructure.database import AsyncDatabase
from ai_butler.workers.runtime import run_polling_worker

logger = logging.getLogger(__name__)


def _warn_multiple_local_workers(app_env: str) -> None:
    """开发环境启动时提示配置漂移风险；生产进程编排不做本机进程推断。"""

    if app_env not in {"development", "test"}:
        return
    pgrep_path = shutil.which("pgrep")
    if pgrep_path is None:
        return
    try:
        # 可执行文件由 PATH 解析，但参数完全固定且不经过 shell 或用户输入。
        result = subprocess.run(  # noqa: S603
            [pgrep_path, "-f", "butler-agent-worker|ai_butler.workers.agent"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return
    worker_pids = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if len(worker_pids) > 1:
        logger.warning(
            "multiple_local_agent_workers_detected count=%s; stop old workers before validation",
            len(worker_pids),
        )


async def run() -> None:
    settings = get_settings()
    _warn_multiple_local_workers(settings.app_env)
    if settings.model_routing_enabled:
        routing_bytes = settings.model_routing_file.read_bytes()
        routing = load_model_routing(settings.model_routing_file, settings.app_env)
        planner = routing.routes["planner"]
        executor = routing.routes["executor"]
        logger.info(
            "agent_worker_model_routing fingerprint=%s planner_timeout_ms=%s "
            "planner_max_output_tokens=%s planner_thinking=%s executor_timeout_ms=%s "
            "executor_max_output_tokens=%s executor_thinking=%s",
            hashlib.sha256(routing_bytes).hexdigest()[:16],
            planner.timeout_ms,
            planner.max_output_tokens,
            planner.thinking.value,
            executor.timeout_ms,
            executor.max_output_tokens,
            executor.thinking.value,
        )
    database = AsyncDatabase(settings.app_database_url)
    service = ButlerService(database, settings)
    worker_id = uuid4()

    async def poll_once() -> None:
        if not await service.worker_poll_once(worker_id):
            logger.debug("agent_worker_idle worker_id=%s", worker_id)

    try:
        await run_polling_worker("agent", settings.worker_poll_interval_ms, poll_once)
    finally:
        await database.close()


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(run())


if __name__ == "__main__":
    main()
