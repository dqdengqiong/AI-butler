from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text

from ai_butler.agent.runtime import DEFAULT_CAPABILITY_REGISTRY
from ai_butler.api.schemas import (
    SendMessageRequest,
)
from ai_butler.domain.errors import ButlerError, conflict, not_found

from .context import ButlerContext
from .conversation_repository import ConversationRepository
from .events import EventService
from .routing import RoutingService
from .shared import (
    NON_TERMINAL_RUN_SQL,
    _json,
    _message_request_hash,
    _row,
)
from .support import ResponseFactory, safe_summary


class MessageService:
    def __init__(
        self,
        context: ButlerContext,
        routing: RoutingService,
        repository: ConversationRepository,
        events: EventService,
        responses: ResponseFactory,
    ) -> None:
        self.database = context.database
        self._preflight_conversation_route = routing._preflight_conversation_route
        self._resolve_message_conversation = routing._resolve_message_conversation
        self._reserve_execution_slot = repository._reserve_execution_slot
        self._append_event = events._append_event
        self._safe_summary = safe_summary
        self._send_response = responses._send_response

    async def send_message(self, user_id: UUID, request: SendMessageRequest) -> dict[str, object]:
        """自动解析会话边界并提交消息；归档、创建和消息写入原子完成。"""

        preflight_route = await self._preflight_conversation_route(user_id, request)
        now = datetime.now(UTC)
        async with self.database.transaction() as connection:
            owner = _row(
                await connection.execute(
                    text("SELECT id FROM users WHERE id=:user_id FOR UPDATE"),
                    {"user_id": user_id},
                )
            )
            if owner is None:
                raise not_found()
            duplicate = _row(
                await connection.execute(
                    text(
                        "SELECT m.id AS user_message_id,m.client_request_hash,m.conversation_id,"
                        "r.id AS run_id,r.pending_response_message_id,r.status,r.attempt "
                        "FROM messages m JOIN agent_runs r ON r.trigger_message_id=m.id "
                        "WHERE m.user_id=:user_id AND m.client_message_id=:client_id"
                    ),
                    {
                        "user_id": user_id,
                        "client_id": request.client_message_id,
                    },
                )
            )
            if duplicate:
                if duplicate.pop("client_request_hash") != _message_request_hash(request):
                    raise conflict("IDEMPOTENCY_KEY_REUSED", "消息标识已用于不同内容，请重新发送")
                conversation = _row(
                    await connection.execute(
                        text("SELECT * FROM conversations WHERE id=:id AND user_id=:user_id"),
                        {"id": duplicate["conversation_id"], "user_id": user_id},
                    )
                )
                if conversation is None:
                    raise not_found()
                response = self._send_response(conversation, duplicate)
                response["transition"] = {
                    "kind": "CONTINUED",
                    "archived_conversation_id": None,
                }
                return response

            conversation, transition = await self._resolve_message_conversation(
                connection, user_id, request, now, preflight_route
            )
            active = _row(
                await connection.execute(
                    text(
                        f"SELECT * FROM agent_runs WHERE conversation_id=:conversation_id "  # noqa: S608
                        f"AND status IN ({NON_TERMINAL_RUN_SQL}) "
                        "FOR UPDATE"
                    ),
                    {"conversation_id": conversation["id"]},
                )
            )
            await self._reserve_execution_slot(
                connection,
                user_id,
                UUID(str(active["id"])) if active else None,
                request.execution_policy,
            )
            if active and active["status"] != "AWAITING_INPUT":
                error_by_status = {
                    "AWAITING_APPROVAL": ("APPROVAL_REQUIRED", "请使用计划卡片完成审批"),
                    "FAILED_RETRYABLE": ("RUN_RETRY_REQUIRED", "请先重试或取消当前运行"),
                }
                code, message = error_by_status.get(
                    active["status"], ("CONVERSATION_BUSY", "管家正在处理上一条消息")
                )
                raise conflict(code, message)
            submitted_content = request.content.strip()
            submitted_context: dict[str, object] | None = None
            if request.selection is not None:
                if active is None:
                    raise ButlerError("SELECTION_NOT_EXPECTED", "当前没有等待选择的运行", 409)
                prompt = _row(
                    await connection.execute(
                        text(
                            "SELECT structured_content FROM messages WHERE id=:id AND user_id=:user_id"
                        ),
                        {"id": active["pending_response_message_id"], "user_id": user_id},
                    )
                )
                cards = (prompt or {}).get("structured_content") or {}
                card_items = cards.get("cards", []) if isinstance(cards, dict) else []
                selected_card = next(
                    (
                        card
                        for card in card_items
                        if isinstance(card, dict)
                        and str(card.get("card_id")) == str(request.selection.card_id)
                        and card.get("card_type") == "SelectionCard"
                    ),
                    None,
                )
                if selected_card is None:
                    raise ButlerError("INVALID_SELECTION", "选择卡不属于当前等待输入", 409)
                actions = selected_card.get("actions", [])
                if not any(
                    isinstance(action, dict)
                    and action.get("action_id") == request.selection.action_id
                    for action in actions
                ):
                    raise ButlerError("INVALID_SELECTION_ACTION", "选择动作无效", 400)
                payload = selected_card.get("payload", {})
                options = payload.get("options", []) if isinstance(payload, dict) else []
                by_id = {
                    str(option.get("id")): option
                    for option in options
                    if isinstance(option, dict) and option.get("id")
                }
                if any(
                    option_id not in by_id for option_id in request.selection.selected_option_ids
                ):
                    raise ButlerError("INVALID_SELECTION_OPTION", "选择项无效", 400)
                if (
                    payload.get("input_mode") == "NATURAL_LANGUAGE"
                    and len(request.selection.selected_option_ids) != 1
                ):
                    raise ButlerError("INVALID_SELECTION_COUNT", "学习时间只能提交一个选择", 400)
                labels = [
                    str(by_id[option_id].get("label", option_id))
                    for option_id in request.selection.selected_option_ids
                ]
                submitted_content = submitted_content or f"已选择：{'、'.join(labels)}"
                submitted_context = {
                    "selection": request.selection.model_dump(mode="json"),
                    "labels": labels,
                    # 卡片和选项均由服务端从当前等待消息重新读取；worker 只消费这份
                    # 可信快照，不接受客户端自行提交的解析结果。
                    "card_phase": payload.get("phase"),
                    "selected_options": [
                        by_id[option_id] for option_id in request.selection.selected_option_ids
                    ],
                    "interpretation": payload.get("interpretation"),
                }
                payload["submitted"] = True
                payload["submitted_option_ids"] = request.selection.selected_option_ids
                await connection.execute(
                    text(
                        "UPDATE messages SET structured_content=CAST(:cards AS jsonb),updated_at=now() "
                        "WHERE id=:id AND user_id=:user_id"
                    ),
                    {
                        "cards": _json(cards),
                        "id": active["pending_response_message_id"],
                        "user_id": user_id,
                    },
                )
            elif active and submitted_content:
                prompt = _row(
                    await connection.execute(
                        text(
                            "SELECT structured_content FROM messages WHERE id=:id AND user_id=:user_id"
                        ),
                        {"id": active["pending_response_message_id"], "user_id": user_id},
                    )
                )
                cards = (prompt or {}).get("structured_content") or {}
                card_items = cards.get("cards", []) if isinstance(cards, dict) else []
                changed = False
                for card in card_items:
                    if not isinstance(card, dict) or card.get("card_type") != "SelectionCard":
                        continue
                    payload = card.get("payload")
                    if (
                        isinstance(payload, dict)
                        and payload.get("input_mode") == "NATURAL_LANGUAGE"
                    ):
                        payload["submitted"] = True
                        payload["answered_by_text"] = True
                        changed = True
                if changed:
                    await connection.execute(
                        text(
                            "UPDATE messages SET structured_content=CAST(:cards AS jsonb),updated_at=now() "
                            "WHERE id=:id AND user_id=:user_id"
                        ),
                        {
                            "cards": _json(cards),
                            "id": active["pending_response_message_id"],
                            "user_id": user_id,
                        },
                    )
            segment_id = UUID(str(conversation["active_segment_id"]))
            user_message_id = uuid4()
            assistant_message_id = uuid4()
            if active:
                run_id = UUID(str(active["id"]))
                execution_mode = "INPUT_RESUME"
                await connection.execute(
                    text(
                        "UPDATE agent_runs SET pending_message_id=:message_id,pending_response_message_id=:response_id,"
                        "status='QUEUED',pending_action='INPUT_RESUME',pending_action_key=:action_key,updated_at=:now "
                        "WHERE id=:run_id"
                    ),
                    {
                        "message_id": user_message_id,
                        "response_id": assistant_message_id,
                        "action_key": f"input:{request.client_message_id}",
                        "now": now,
                        "run_id": run_id,
                    },
                )
            else:
                run_id = uuid4()
                execution_mode = "START"
            await connection.execute(
                text(
                    "INSERT INTO messages(id,user_id,conversation_id,segment_id,agent_run_id,client_message_id,"
                    "client_request_hash,role,status,content,structured_content,created_at) "
                    "VALUES(:id,:user_id,:conversation_id,:segment_id,:run_id,:client_id,:request_hash,"
                    "'USER','COMPLETED',:content,CAST(:structured AS jsonb),:created_at)"
                ),
                {
                    "id": user_message_id,
                    "user_id": user_id,
                    "conversation_id": conversation["id"],
                    "segment_id": segment_id,
                    "run_id": run_id,
                    "client_id": request.client_message_id,
                    "request_hash": _message_request_hash(request),
                    "content": submitted_content,
                    "structured": _json(submitted_context or {}),
                    "created_at": now,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO memory_extraction_jobs(id,user_id,message_id,status) "
                    "VALUES(:id,:user_id,:message_id,'PENDING') ON CONFLICT(message_id) DO NOTHING"
                ),
                {"id": uuid4(), "user_id": user_id, "message_id": user_message_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO messages(id,user_id,conversation_id,segment_id,agent_run_id,role,status,content,created_at) "
                    "VALUES(:id,:user_id,:conversation_id,:segment_id,:run_id,'ASSISTANT','PENDING','',:created_at)"
                ),
                {
                    "id": assistant_message_id,
                    "user_id": user_id,
                    "conversation_id": conversation["id"],
                    "segment_id": segment_id,
                    "run_id": run_id,
                    "created_at": now + timedelta(microseconds=1),
                },
            )
            if not active:
                await connection.execute(
                    text(
                        "INSERT INTO agent_runs(id,user_id,conversation_id,segment_id,trigger_message_id,"
                        "pending_message_id,pending_response_message_id,status,pending_action,pending_action_key,"
                        "input_summary,selected_user_agent_id,capability_registry_fingerprint,trace_id) "
                        "VALUES(:id,:user_id,:conversation_id,:segment_id,:request_id,:request_id,:response_id,'QUEUED',"
                        "'START',:action_key,:summary,:selected_agent,:registry_fingerprint,:trace_id)"
                    ),
                    {
                        "id": run_id,
                        "user_id": user_id,
                        "conversation_id": conversation["id"],
                        "segment_id": segment_id,
                        "request_id": user_message_id,
                        "response_id": assistant_message_id,
                        "action_key": f"start:{request.client_message_id}",
                        "summary": self._safe_summary(request.content),
                        "selected_agent": conversation["specialist_user_agent_id"],
                        "registry_fingerprint": DEFAULT_CAPABILITY_REGISTRY.fingerprint,
                        "trace_id": str(uuid4()),
                    },
                )
            for attachment in request.attachments:
                ready = _row(
                    await connection.execute(
                        text(
                            "SELECT id FROM stored_files WHERE id=:id AND user_id=:user_id "
                            "AND upload_status='VERIFIED' AND scan_status='CLEAN'"
                        ),
                        {"id": attachment.file_id, "user_id": user_id},
                    )
                )
                if ready is None:
                    raise ButlerError("FILE_NOT_READY", "附件尚未完成安全验证", 409)
                await connection.execute(
                    text(
                        "INSERT INTO message_attachments(id,message_id,stored_file_id,user_id,position) "
                        "VALUES(:id,:message,:file,:user_id,:position)"
                    ),
                    {
                        "id": uuid4(),
                        "message": user_message_id,
                        "file": attachment.file_id,
                        "user_id": user_id,
                        "position": attachment.position,
                    },
                )
            title = str(conversation["title"])
            user_message_count = int(
                (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM messages "
                            "WHERE conversation_id=:conversation_id AND role='USER'"
                        ),
                        {"conversation_id": conversation["id"]},
                    )
                ).scalar_one()
            )
            if user_message_count == 1:
                title_seed = submitted_content.strip()
                if title_seed:
                    normalized_title = re.sub(r"\s+", " ", title_seed)[:24]
                    title = (
                        f"考公 · {normalized_title}"
                        if conversation["specialist_user_agent_id"]
                        else normalized_title
                    )
                elif request.attachments:
                    title = "资料对话"
            await connection.execute(
                text(
                    "UPDATE conversations SET title=:title,last_message_at=:now,updated_at=:now WHERE id=:id"
                ),
                {"title": title, "now": now, "id": conversation["id"]},
            )
            sequence = await self._append_event(
                connection,
                run_id,
                user_id,
                "run.accepted",
                {"status": "QUEUED", "execution_mode": execution_mode},
                int(active["attempt"]) if active else 0,
            )
            result = {
                "user_message_id": user_message_id,
                "run_id": run_id,
                "pending_response_message_id": assistant_message_id,
                "status": "QUEUED",
                "attempt": int(active["attempt"]) if active else 0,
            }
        response = self._send_response(conversation, result)
        response["transition"] = transition
        response["run"]["execution_mode"] = execution_mode  # type: ignore[index]
        response["stream"]["last_sequence"] = sequence  # type: ignore[index]
        return response
