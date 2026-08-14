"""AI 管家验证版公共 `/v1` API。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from ai_butler.adapters.auth import MockWechatAuthProvider, WechatCodeAuthProvider
from ai_butler.api.dependencies import Butler, CurrentUserId
from ai_butler.api.schemas import (
    AgentDefinitionListResponse,
    ApprovalDecisionRequest,
    AvailabilityRequest,
    CitationResponseV1,
    CompleteUploadRequest,
    ConversationListResponse,
    ConversationResponse,
    CreateConversationRequest,
    DeleteAccountRequest,
    LogoutRequest,
    MessageListResponse,
    PreferencesRequest,
    ProfileRequest,
    RefreshRequest,
    RetryRunRequest,
    SendMessageRequest,
    SendMessageResponse,
    TaskExecutionRequest,
    UpdateMeRequest,
    UploadIntentRequest,
    WechatLoginRequest,
)
from ai_butler.domain.errors import ButlerError
from ai_butler.security import InvalidTokenError, verify_signed_ticket

router = APIRouter(prefix="/v1")


@router.post("/auth/wechat/login", tags=["auth"])
async def wechat_login(
    payload: WechatLoginRequest,
    request: Request,
    butler: Butler,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    del idempotency_key
    settings = request.app.state.settings
    provider = (
        MockWechatAuthProvider()
        if settings.wechat_auth_mode == "mock"
        else WechatCodeAuthProvider(settings.wechat_app_id, settings.wechat_app_secret)
    )
    try:
        identity = await provider.exchange(payload.login_code)
    except ValueError as exc:
        raise ButlerError("WECHAT_LOGIN_FAILED", "微信登录失败，请重试", 401, True) from exc
    return await butler.login(identity, payload.device_id)


@router.post("/auth/refresh", tags=["auth"])
async def refresh(payload: RefreshRequest, butler: Butler) -> dict[str, object]:
    return await butler.refresh(payload.refresh_token, payload.device_id)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
async def logout(payload: LogoutRequest, user_id: CurrentUserId, butler: Butler) -> Response:
    await butler.logout(user_id, payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", tags=["user"])
async def get_me(user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.get_me(user_id)


@router.patch("/me", tags=["user"])
async def update_me(
    payload: UpdateMeRequest, user_id: CurrentUserId, butler: Butler
) -> dict[str, object]:
    return await butler.update_me(user_id, payload)


@router.get("/me/profile", tags=["user"])
async def get_profile(user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.get_profile(user_id)


@router.put("/me/profile", tags=["user"])
async def put_profile(
    payload: ProfileRequest, user_id: CurrentUserId, butler: Butler
) -> dict[str, object]:
    return await butler.put_profile(user_id, payload)


@router.get("/me/availability", tags=["user"])
async def get_availability(user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.get_availability(user_id)


@router.put("/me/availability", tags=["user"])
async def put_availability(
    payload: AvailabilityRequest, user_id: CurrentUserId, butler: Butler
) -> dict[str, object]:
    return await butler.put_availability(user_id, payload)


@router.get("/me/preferences", tags=["user"])
async def get_preferences(user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.get_preferences(user_id)


@router.patch("/me/preferences", tags=["user"])
async def patch_preferences(
    payload: PreferencesRequest, user_id: CurrentUserId, butler: Butler
) -> dict[str, object]:
    return await butler.patch_preferences(user_id, payload)


@router.delete("/me", status_code=status.HTTP_202_ACCEPTED, tags=["user"])
async def delete_me(
    payload: DeleteAccountRequest,
    user_id: CurrentUserId,
    butler: Butler,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    del payload, idempotency_key
    return await butler.delete_account(user_id)


@router.get("/dashboard", tags=["planning"])
async def dashboard(
    user_id: CurrentUserId,
    butler: Butler,
    requested_date: date | None = Query(default=None, alias="date"),
) -> dict[str, object]:
    return await butler.dashboard(user_id, requested_date or datetime.now(UTC).date())


@router.get("/goals", tags=["planning"])
async def goals(user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.list_goals(user_id)


@router.get("/plans", tags=["planning"])
async def plans(user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.list_plans(user_id)


@router.get("/plans/{plan_id}", tags=["planning"])
async def plan(plan_id: UUID, user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.get_plan(user_id, plan_id)


@router.get("/plans/{plan_id}/revisions", tags=["planning"])
async def revisions(plan_id: UUID, user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.list_revisions(user_id, plan_id)


@router.get("/plans/{plan_id}/revisions/{revision_id}", tags=["planning"])
async def revision(
    plan_id: UUID, revision_id: UUID, user_id: CurrentUserId, butler: Butler
) -> dict[str, object]:
    return await butler.get_revision(user_id, plan_id, revision_id)


@router.get("/tasks", tags=["planning"])
async def tasks(
    user_id: CurrentUserId,
    butler: Butler,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, object]:
    return await butler.list_tasks(user_id, date_from, date_to)


@router.get("/tasks/{task_id}", tags=["planning"])
async def task(task_id: UUID, user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.get_task(user_id, task_id)


@router.post("/tasks/{task_id}/executions", status_code=status.HTTP_201_CREATED, tags=["planning"])
async def execute_task(
    task_id: UUID,
    payload: TaskExecutionRequest,
    user_id: CurrentUserId,
    butler: Butler,
) -> dict[str, object]:
    return await butler.execute_task(user_id, task_id, payload)


@router.get("/agent-definitions", response_model=AgentDefinitionListResponse, tags=["agents"])
async def agent_definitions(butler: Butler) -> dict[str, object]:
    """返回公开专业入口目录；不要求登录且不暴露内部执行标识。"""

    return await butler.list_agent_definitions()


@router.get("/conversations", response_model=ConversationListResponse, tags=["conversations"])
async def conversations(
    user_id: CurrentUserId,
    butler: Butler,
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = None,
) -> dict[str, object]:
    return await butler.list_conversations(user_id, limit, cursor)


@router.post(
    "/conversations",
    status_code=status.HTTP_201_CREATED,
    response_model=ConversationResponse,
    tags=["conversations"],
)
async def create_conversation(
    payload: CreateConversationRequest, user_id: CurrentUserId, butler: Butler
) -> dict[str, object]:
    return await butler.create_conversation(user_id, payload)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    tags=["conversations"],
)
async def conversation(
    conversation_id: UUID, user_id: CurrentUserId, butler: Butler
) -> dict[str, object]:
    return await butler.get_conversation(user_id, conversation_id)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=MessageListResponse,
    tags=["conversations"],
)
async def messages(
    conversation_id: UUID,
    user_id: CurrentUserId,
    butler: Butler,
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = None,
) -> dict[str, object]:
    return await butler.list_messages(user_id, conversation_id, limit, cursor)


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SendMessageResponse,
    tags=["conversations"],
)
async def send_message(
    conversation_id: UUID,
    payload: SendMessageRequest,
    user_id: CurrentUserId,
    butler: Butler,
) -> dict[str, object]:
    return await butler.send_message(user_id, conversation_id, payload)


@router.get("/agent-runs/{run_id}", tags=["chat"])
async def get_run(run_id: UUID, user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.get_run(user_id, run_id)


@router.post("/agent-runs/{run_id}/stream-ticket", tags=["chat"])
async def stream_ticket(run_id: UUID, user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.stream_ticket(user_id, run_id)


@router.get("/agent-runs/{run_id}/events", tags=["chat"])
async def events(
    run_id: UUID,
    butler: Butler,
    ticket: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    try:
        verify_signed_ticket(ticket, run_id, butler.settings.stream_ticket_secret)
    except InvalidTokenError as exc:
        raise ButlerError("INVALID_STREAM_TICKET", "流票据无效或已过期", 401) from exc
    user_id = await butler.event_owner(run_id)
    cursor = max(after, int(last_event_id or 0))

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        idle_seconds = 0
        while idle_seconds < 45:
            batch = await butler.list_events(user_id, run_id, cursor)
            if batch:
                idle_seconds = 0
                for item in batch:
                    cursor = int(str(item["sequence"]))
                    payload = {
                        "schema_version": "1.0",
                        "run_id": str(run_id),
                        "sequence": cursor,
                        "attempt": item["attempt"],
                        "payload": item["payload"],
                    }
                    yield (
                        f"id: {cursor}\nevent: {item['event_type']}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    )
                continue
            await asyncio.sleep(butler.settings.sse_poll_interval_ms / 1000)
            idle_seconds += max(1, butler.settings.sse_poll_interval_ms // 1000)
            if idle_seconds and int(idle_seconds) % butler.settings.sse_heartbeat_seconds == 0:
                yield ": heartbeat\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/agent-runs/{run_id}/cancel", tags=["chat"])
async def cancel_run(
    run_id: UUID,
    user_id: CurrentUserId,
    butler: Butler,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    del idempotency_key
    return await butler.cancel_run(user_id, run_id)


@router.post("/agent-runs/{run_id}/retry", tags=["chat"])
async def retry_run(
    run_id: UUID, payload: RetryRunRequest, user_id: CurrentUserId, butler: Butler
) -> dict[str, object]:
    return await butler.retry_run(user_id, run_id, payload.expected_attempt)


@router.post("/approvals/{approval_id}/decisions", tags=["chat"])
async def decide(
    approval_id: UUID,
    payload: ApprovalDecisionRequest,
    user_id: CurrentUserId,
    butler: Butler,
) -> dict[str, object]:
    return await butler.decide_approval(user_id, approval_id, payload)


@router.post("/files/upload-intents", status_code=status.HTTP_201_CREATED, tags=["files"])
async def upload_intent(
    payload: UploadIntentRequest,
    user_id: CurrentUserId,
    butler: Butler,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    del idempotency_key
    return await butler.create_upload_intent(user_id, payload)


@router.put("/files/{file_id}/content", include_in_schema=False)
async def put_file_content(
    file_id: UUID, ticket: str, request: Request, butler: Butler
) -> Response:
    try:
        verify_signed_ticket(ticket, file_id, butler.settings.stream_ticket_secret)
    except InvalidTokenError as exc:
        raise ButlerError("INVALID_UPLOAD_TICKET", "上传票据无效或已过期", 401) from exc
    await butler.store_local_upload(file_id, await request.body())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/files/{file_id}/content", include_in_schema=False)
async def get_file_content(file_id: UUID, ticket: str, butler: Butler) -> Response:
    try:
        verify_signed_ticket(ticket, file_id, butler.settings.stream_ticket_secret)
    except InvalidTokenError as exc:
        raise ButlerError("INVALID_DOWNLOAD_TICKET", "下载票据无效或已过期", 401) from exc
    content, mime, filename = await butler.read_local_file(file_id)
    safe_filename = filename.encode("ascii", "ignore").decode() or "file"
    return Response(
        content=content,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
    )


@router.post("/files/{file_id}/complete", status_code=status.HTTP_202_ACCEPTED, tags=["files"])
async def complete_file(
    file_id: UUID,
    payload: CompleteUploadRequest,
    user_id: CurrentUserId,
    butler: Butler,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    del idempotency_key
    return await butler.complete_upload(user_id, file_id, payload)


@router.get("/files", tags=["files"])
async def list_files(user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.list_files(user_id)


@router.get("/files/{file_id}", tags=["files"])
async def get_file(file_id: UUID, user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.get_file(user_id, file_id)


@router.get("/files/{file_id}/download-url", tags=["files"])
async def download_file(file_id: UUID, user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.file_download(user_id, file_id)


@router.delete("/files/{file_id}", status_code=status.HTTP_202_ACCEPTED, tags=["files"])
async def delete_file(file_id: UUID, user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    return await butler.delete_file(user_id, file_id)


@router.get("/citations/{citation_id}", response_model=CitationResponseV1, tags=["knowledge"])
async def citation(citation_id: UUID, user_id: CurrentUserId, butler: Butler) -> dict[str, object]:
    """读取当前用户回答中的引用快照和安全访问方式。"""

    return await butler.get_citation(user_id, citation_id)


@router.get("/knowledge-documents/{document_id}/access-url", tags=["knowledge"])
async def document_access(
    document_id: UUID, user_id: CurrentUserId, butler: Butler
) -> dict[str, object]:
    return await butler.get_document_access(user_id, document_id)
