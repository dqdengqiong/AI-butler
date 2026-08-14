from uuid import UUID

import pytest

from ai_butler.adapters.auth import MockWechatAuthProvider
from ai_butler.adapters.notification import Notification, RecordingNotificationProvider


async def test_mock_wechat_identity_is_deterministic() -> None:
    provider = MockWechatAuthProvider()
    first = await provider.exchange("developer-a")
    second = await provider.exchange("developer-a")
    assert first == second
    assert first.provider == "WECHAT_MOCK"


async def test_mock_wechat_rejects_empty_code() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        await MockWechatAuthProvider().exchange("  ")


async def test_recording_notification_is_idempotent() -> None:
    provider = RecordingNotificationProvider()
    first = Notification(
        user_id=UUID("00000000-0000-4000-8000-000000000001"),
        template="daily-task",
        idempotency_key="notification-1",
    )
    duplicate = Notification(
        user_id=first.user_id,
        template="changed-template",
        idempotency_key=first.idempotency_key,
    )
    assert await provider.send(first) == "notification-1"
    assert await provider.send(duplicate) == "notification-1"
    assert provider.sent == {"notification-1": first}
