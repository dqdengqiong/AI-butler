import asyncio

import pytest

from ai_butler.workers.runtime import run_polling_worker


async def test_polling_worker_polls_before_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def poll_once() -> None:
        calls.append("poll")

    async def stop_after_first_poll(delay: float) -> None:
        assert delay == 0.25
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_first_poll)

    with pytest.raises(asyncio.CancelledError):
        await run_polling_worker("test", 250, poll_once)
    assert calls == ["poll"]
