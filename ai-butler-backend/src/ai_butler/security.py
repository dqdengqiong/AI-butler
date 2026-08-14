from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class ResourceNotFoundError(LookupError):
    """Used for both absent and unauthorized resources to avoid ownership disclosure."""


class InvalidTokenError(ValueError):
    """令牌无效、签名不匹配或已经过期。"""


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """服务端签发并验证后的短期访问令牌声明。"""

    user_id: UUID
    session_id: UUID
    expires_at: int


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def _sign(value: str, secret: str) -> str:
    return _b64encode(hmac.new(secret.encode(), value.encode(), hashlib.sha256).digest())


def issue_access_token(
    user_id: UUID,
    session_id: UUID,
    secret: str,
    lifetime_seconds: int,
    *,
    now: int | None = None,
) -> str:
    """签发最小化 HS256 风格访问令牌，不在载荷中放置用户资料。"""

    issued_at = int(time.time()) if now is None else now
    header = _b64encode(b'{"alg":"HS256","typ":"JWT"}')
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "sid": str(session_id),
        "iat": issued_at,
        "exp": issued_at + lifetime_seconds,
        "v": 1,
    }
    encoded_payload = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    unsigned = f"{header}.{encoded_payload}"
    return f"{unsigned}.{_sign(unsigned, secret)}"


def verify_access_token(token: str, secret: str, *, now: int | None = None) -> AccessTokenClaims:
    """验证访问令牌签名、版本、主体和到期时间。"""

    try:
        header, payload, signature = token.split(".")
        unsigned = f"{header}.{payload}"
        if not hmac.compare_digest(signature, _sign(unsigned, secret)):
            raise InvalidTokenError("invalid access token")
        data = json.loads(_b64decode(payload))
        current = int(time.time()) if now is None else now
        if data.get("v") != 1 or int(data["exp"]) <= current:
            raise InvalidTokenError("expired access token")
        return AccessTokenClaims(
            user_id=UUID(data["sub"]),
            session_id=UUID(data["sid"]),
            expires_at=int(data["exp"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, InvalidTokenError):
            raise
        raise InvalidTokenError("invalid access token") from exc


def issue_refresh_token(session_id: UUID) -> str:
    """生成带会话定位符的随机刷新令牌；随机部分只返回给客户端。"""

    return f"{session_id}.{secrets.token_urlsafe(32)}"


def refresh_token_session_id(token: str) -> UUID:
    """解析刷新令牌的会话定位符，随机部分仍需通过 HMAC 比较验证。"""

    try:
        session_id, secret = token.split(".", 1)
        if len(secret) < 32:
            raise ValueError
        return UUID(session_id)
    except ValueError as exc:
        raise InvalidTokenError("invalid refresh token") from exc


def token_hmac(token: str, secret: str) -> str:
    """计算可安全持久化的令牌 HMAC，数据库永不保存明文刷新令牌。"""

    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def issue_signed_ticket(subject: UUID, secret: str, lifetime_seconds: int) -> str:
    """签发短期流或本地文件票据。"""

    expires_at = int(time.time()) + lifetime_seconds
    payload = _b64encode(f"{subject}:{expires_at}".encode())
    return f"{payload}.{_sign(payload, secret)}"


def verify_signed_ticket(ticket: str, subject: UUID, secret: str) -> None:
    """验证票据主体和到期时间；错误统一处理，避免暴露资源存在性。"""

    try:
        payload, signature = ticket.split(".", 1)
        if not hmac.compare_digest(signature, _sign(payload, secret)):
            raise InvalidTokenError("invalid ticket")
        raw_subject, raw_expiry = _b64decode(payload).decode().split(":", 1)
        if UUID(raw_subject) != subject or int(raw_expiry) <= int(time.time()):
            raise InvalidTokenError("invalid ticket")
    except (ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, InvalidTokenError):
            raise
        raise InvalidTokenError("invalid ticket") from exc


def require_owner(authenticated_user_id: UUID, resource_user_id: UUID) -> None:
    if authenticated_user_id != resource_user_id:
        raise ResourceNotFoundError("resource not found")


def qdrant_user_filter(authenticated_user_id: UUID) -> dict[str, object]:
    return {
        "must": [
            {
                "key": "tenant_id",
                "match": {"value": str(authenticated_user_id)},
            }
        ]
    }
