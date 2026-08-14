"""FastAPI 认证与应用服务依赖。"""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, Header, Request

from ai_butler.application.butler import ButlerService
from ai_butler.domain.errors import ButlerError
from ai_butler.security import InvalidTokenError, verify_access_token


def get_butler(request: Request) -> ButlerService:
    """取得应用生命周期内共享、无请求状态的用例服务。"""

    return cast(ButlerService, request.app.state.butler)


def current_user_id(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> UUID:
    """仅从已验证 Bearer Token 解析用户身份，禁止信任请求体 user_id。"""

    if authorization is None or not authorization.startswith("Bearer "):
        raise ButlerError("AUTHENTICATION_REQUIRED", "请先登录", 401)
    try:
        claims = verify_access_token(
            authorization.removeprefix("Bearer ").strip(),
            request.app.state.settings.auth_access_token_secret,
        )
    except InvalidTokenError as exc:
        raise ButlerError("INVALID_ACCESS_TOKEN", "登录状态已失效", 401) from exc
    return claims.user_id


CurrentUserId = Annotated[UUID, Depends(current_user_id)]
Butler = Annotated[ButlerService, Depends(get_butler)]
