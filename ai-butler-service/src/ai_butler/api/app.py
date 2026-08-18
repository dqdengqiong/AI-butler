from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ai_butler import __version__
from ai_butler.adapters.auth import MockWechatAuthProvider, WechatCodeAuthProvider
from ai_butler.api.routers.health import router as health_router
from ai_butler.api.routers.v1 import router as v1_router
from ai_butler.application.butler import ButlerService
from ai_butler.config import Settings, get_settings
from ai_butler.domain.errors import ButlerError
from ai_butler.infrastructure.database import AsyncDatabase, Database


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    if not resolved_settings.sms_verification_enabled:
        logging.getLogger(__name__).warning(
            "SMS verification is disabled; phone login accepts a validated "
            "phone format without a code"
        )
    resolved_database = database or AsyncDatabase(resolved_settings.app_database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.database = resolved_database
        yield
        await resolved_database.close()

    application = FastAPI(
        title="AI Butler API",
        version="1.1.0",
        description="AI个人管家后端公共 API",
        lifespan=lifespan,
    )
    application.state.database = resolved_database
    application.state.settings = resolved_settings
    application.state.wechat_auth_provider = (
        MockWechatAuthProvider()
        if resolved_settings.wechat_auth_mode == "mock"
        else WechatCodeAuthProvider(
            resolved_settings.wechat_app_id, resolved_settings.wechat_app_secret
        )
    )
    application.state.butler = ButlerService(resolved_database, resolved_settings)  # type: ignore[arg-type]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.app_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(health_router)
    application.include_router(v1_router)
    application.state.package_version = __version__

    @application.middleware("http")
    async def request_context(request: Request, call_next: object) -> JSONResponse:
        """传播安全 request_id；不记录请求体、令牌或流票据。"""

        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)  # type: ignore[operator]
        response.headers["X-Request-ID"] = request_id
        return response  # type: ignore[no-any-return]

    @application.exception_handler(ButlerError)
    async def handle_butler_error(request: Request, error: ButlerError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "request_id": getattr(request.state, "request_id", str(uuid4())),
                    "retryable": error.retryable,
                    "details": error.details,
                }
            },
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        del error
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求参数不符合接口约束",
                    "request_id": getattr(request.state, "request_id", str(uuid4())),
                    "retryable": False,
                    "details": {},
                }
            },
        )

    return application


app = create_app()
