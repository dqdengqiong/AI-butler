"""短信验证码发送适配器边界。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID


class SmsProvider(Protocol):
    """发送登录验证码；实现不得记录手机号或验证码。"""

    async def send_login_code(self, phone: str, code: str, challenge_id: UUID) -> str: ...


@dataclass(slots=True)
class MockSmsProvider:
    """仅用于本地与自动化验证的记录型短信适配器。"""

    sent_challenges: set[UUID] = field(default_factory=set)

    async def send_login_code(self, phone: str, code: str, challenge_id: UUID) -> str:
        del phone, code
        self.sent_challenges.add(challenge_id)
        return f"mock-{challenge_id}"
