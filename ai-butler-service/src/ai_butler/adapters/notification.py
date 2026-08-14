from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Notification:
    user_id: UUID
    template: str
    idempotency_key: str


class NotificationProvider(Protocol):
    async def send(self, notification: Notification) -> str: ...


@dataclass(slots=True)
class RecordingNotificationProvider:
    sent: dict[str, Notification] = field(default_factory=dict)

    async def send(self, notification: Notification) -> str:
        self.sent.setdefault(notification.idempotency_key, notification)
        return notification.idempotency_key
