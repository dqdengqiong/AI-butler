from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid5

MOCK_USER_NAMESPACE = UUID("927b563c-87af-47f1-b91a-9b031463a20c")


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    provider: str
    subject: str
    user_id: UUID


class AuthProvider(Protocol):
    async def exchange(self, code: str) -> AuthIdentity: ...


class MockWechatAuthProvider:
    async def exchange(self, code: str) -> AuthIdentity:
        if not code.strip():
            raise ValueError("mock login code must not be empty")
        return AuthIdentity(
            provider="WECHAT_MOCK",
            subject=code,
            user_id=uuid5(MOCK_USER_NAMESPACE, code),
        )


class WechatCodeAuthProvider:
    """微信小程序 ``code2session`` 适配器。

    session_key 仅用于确认上游响应有效，绝不返回、持久化或写入日志。
    """

    def __init__(self, app_id: str, app_secret: str) -> None:
        if not app_id or not app_secret:
            raise ValueError("wechat credentials are required")
        self._app_id = app_id
        self._app_secret = app_secret

    async def exchange(self, code: str) -> AuthIdentity:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                "https://api.weixin.qq.com/sns/jscode2session",
                params={
                    "appid": self._app_id,
                    "secret": self._app_secret,
                    "js_code": code,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            payload = response.json()
        openid = payload.get("openid")
        if not isinstance(openid, str) or not openid:
            raise ValueError("wechat identity exchange failed")
        return AuthIdentity(
            provider="WECHAT_MINIAPP",
            subject=openid,
            user_id=uuid5(MOCK_USER_NAMESPACE, f"wechat:{openid}"),
        )
