from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ai_butler.infrastructure.database import Database

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"
    checks: dict[str, Literal["up"]]


class UnreadyResponse(BaseModel):
    status: Literal["unready"] = "unready"
    checks: dict[str, Literal["down"]]


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse()


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": UnreadyResponse}},
)
async def ready(request: Request) -> ReadyResponse | JSONResponse:
    database: Database = request.app.state.database
    if not await database.ping():
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=UnreadyResponse(checks={"postgres": "down"}).model_dump(),
        )
    return ReadyResponse(checks={"postgres": "up"})
