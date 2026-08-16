from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    provider: str
    subject: str
    union_subject: str | None = None


class AuthProvider(Protocol):
    async def exchange(self, code: str) -> AuthIdentity: ...

    async def exchange_phone(self, code: str) -> str: ...


class MockWechatAuthProvider:
    async def exchange(self, code: str) -> AuthIdentity:
        if not code.strip():
            raise ValueError("mock login code must not be empty")
        return AuthIdentity(
            provider="WECHAT_MOCK",
            subject=code,
        )

    async def exchange_phone(self, code: str) -> str:
        if not code.strip():
            raise ValueError("mock phone code must not be empty")
        if code.startswith("phone:"):
            return code.removeprefix("phone:")
        # 验证环境仍需让不同微信主体稳定落到不同合成手机号，但不使用真实号码。
        suffix = int(hashlib.sha256(code.encode()).hexdigest()[:8], 16) % 100_000_000
        return f"139{suffix:08d}"


class WechatCodeAuthProvider:
    """微信小程序 ``code2session`` 适配器。

    session_key 仅用于确认上游响应有效，绝不返回、持久化或写入日志。
    """

    def __init__(self, app_id: str, app_secret: str) -> None:
        if not app_id or not app_secret:
            raise ValueError("wechat credentials are required")
        self._app_id = app_id
        self._app_secret = app_secret
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def exchange(self, code: str) -> AuthIdentity:
        import httpx

        try:
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
        except (httpx.HTTPError, ValueError, TypeError):
            raise ValueError("wechat identity exchange failed") from None
        if not isinstance(payload, dict):
            raise ValueError("wechat identity exchange failed")
        openid = payload.get("openid")
        if not isinstance(openid, str) or not openid:
            raise ValueError("wechat identity exchange failed")
        return AuthIdentity(
            provider="WECHAT_MINIAPP",
            subject=openid,
            union_subject=(
                payload.get("unionid") if isinstance(payload.get("unionid"), str) else None
            ),
        )

    async def exchange_phone(self, code: str) -> str:
        """使用一次性手机号动态 code 获取用户主动授权的号码。"""

        if not code.strip():
            raise ValueError("wechat phone code must not be empty")
        access_token = await self._application_access_token()
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    "https://api.weixin.qq.com/wxa/business/getuserphonenumber",
                    params={"access_token": access_token},
                    json={"code": code},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            raise ValueError("wechat phone exchange failed") from None
        if not isinstance(payload, dict):
            raise ValueError("wechat phone exchange failed")
        phone_info = payload.get("phone_info")
        if payload.get("errcode") not in (None, 0) or not isinstance(phone_info, dict):
            raise ValueError("wechat phone exchange failed")
        phone = phone_info.get("purePhoneNumber") or phone_info.get("phoneNumber")
        if not isinstance(phone, str) or not phone:
            raise ValueError("wechat phone exchange failed")
        return phone

    async def _application_access_token(self) -> str:
        """进程内缓存微信接口凭据，并用锁避免并发登录触发刷新风暴。"""

        if self._access_token and time.monotonic() < self._access_token_expires_at:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._access_token_expires_at:
                return self._access_token
            import httpx

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        "https://api.weixin.qq.com/cgi-bin/stable_token",
                        json={
                            "grant_type": "client_credential",
                            "appid": self._app_id,
                            "secret": self._app_secret,
                            "force_refresh": False,
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError):
                raise ValueError("wechat access token exchange failed") from None
            if not isinstance(payload, dict):
                raise ValueError("wechat access token exchange failed")
            token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not isinstance(token, str) or not token or not isinstance(expires_in, int):
                raise ValueError("wechat access token exchange failed")
            self._access_token = token
            self._access_token_expires_at = time.monotonic() + max(60, expires_in - 300)
            return token
