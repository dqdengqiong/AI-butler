from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import text

from ai_butler.agent.evidence import estimate_tokens
from ai_butler.agent.versions import (
    CURRENT_GRAPH_VERSION,
    CURRENT_PROMPT_BUNDLE_VERSION,
    TOOL_REGISTRY_VERSION,
)
from ai_butler.api.schemas import SendMessageRequest
from ai_butler.domain.errors import ButlerError, conflict, not_found
from ai_butler.tools import DEFAULT_TOOL_REGISTRY

from .context import ButlerContext
from .conversation_repository import ConversationRepository
from .events import EventService
from .routing import RoutingService
from .shared import NON_TERMINAL_RUN_SQL, _json, _message_request_hash, _row
from .support import ResponseFactory, safe_summary


class MessageService:
    """把一条用户消息原子转换为一个不可恢复的单轮 run。"""

    def __init__(
        self,
        context: ButlerContext,
        routing: RoutingService,
        repository: ConversationRepository,
        events: EventService,
        responses: ResponseFactory,
    ) -> None:
        self.database = context.database
        self.settings = context.settings
        self._langgraph_database_url = context.settings.langgraph_database_url
        self._preflight_conversation_route = routing._preflight_conversation_route
        self._resolve_message_conversation = routing._resolve_message_conversation
        self._reserve_execution_slot = repository._reserve_execution_slot
        self._cancel_run_row = repository._cancel_run_row
        self._append_event = events._append_event
        self._safe_summary = safe_summary
        self._send_response = responses._send_response

    async def send_message(
        self,
        user_id: UUID,
        request: SendMessageRequest,
    ) -> dict[str, object]:
        """每次发送都创建新 run；不存在选择提交或旧 run 恢复。"""

        if request.content.strip() in {"清空当前上下文", "清除当前上下文"}:
            request = request.model_copy(update={"context_policy": "ARCHIVE_AND_START"})
        if estimate_tokens(request.content.strip()) > self.settings.message_input_max_tokens:
            raise ButlerError(
                "MESSAGE_TOO_LONG",
                f"单条消息最多约 {self.settings.message_input_max_tokens} Token，请改用附件",
                422,
            )
        preflight_route = await self._preflight_conversation_route(user_id, request)
        now = datetime.now(UTC)
        request_hash = _message_request_hash(request)
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
                    {"user_id": user_id, "client_id": request.client_message_id},
                )
            )
            if duplicate:
                if duplicate.pop("client_request_hash") != request_hash:
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
                response["transition"] = {"kind": "CONTINUED", "archived_conversation_id": None}
                return response

            conversation, transition = await self._resolve_message_conversation(
                connection, user_id, request, now, preflight_route
            )
            active = _row(
                await connection.execute(
                    text(
                        f"SELECT id,status FROM agent_runs WHERE conversation_id=:conversation_id "  # noqa: S608
                        f"AND status IN ({NON_TERMINAL_RUN_SQL}) FOR UPDATE"
                    ),
                    {"conversation_id": conversation["id"]},
                )
            )
            await self._reserve_execution_slot(connection, user_id, None, request.execution_policy)
            if active is not None:
                if active["status"] == "FAILED_RETRYABLE":
                    await self._cancel_run_row(connection, user_id, active, "NEW_MESSAGE")
                elif request.execution_policy != "CANCEL_OTHER":
                    raise conflict("CONVERSATION_BUSY", "管家正在处理上一条消息")

            segment_id = UUID(str(conversation["active_segment_id"]))
            run_id = uuid4()
            user_message_id = uuid4()
            assistant_message_id = uuid4()
            submitted_content = request.content.strip()
            await connection.execute(
                text(
                    "INSERT INTO messages(id,user_id,conversation_id,segment_id,agent_run_id,"
                    "client_message_id,client_request_hash,role,status,content,structured_content,created_at) "
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
                    "request_hash": request_hash,
                    "content": submitted_content,
                    "structured": _json({}),
                    "created_at": now,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO memory_extraction_jobs(id,user_id,message_id,source_conversation_id,"
                    "policy_generation,status) SELECT :id,:user_id,:message_id,:conversation_id,"
                    "COALESCE((SELECT policy_generation FROM memory_policy_state "
                    "WHERE user_id=:user_id),1),'PENDING' ON CONFLICT(message_id) DO NOTHING"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "message_id": user_message_id,
                    "conversation_id": conversation["id"],
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO messages(id,user_id,conversation_id,segment_id,agent_run_id,role,"
                    "status,content,created_at) VALUES(:id,:user_id,:conversation_id,:segment_id,"
                    ":run_id,'ASSISTANT','PENDING','',:created_at)"
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
            await connection.execute(
                text(
                    "INSERT INTO agent_runs(id,user_id,conversation_id,segment_id,trigger_message_id,"
                    "pending_message_id,pending_response_message_id,status,input_summary,"
                    "selected_user_agent_id,graph_version,prompt_bundle_version,"
                    "tool_registry_version,tool_registry_fingerprint,trace_id) "
                    "VALUES(:id,:user_id,:conversation_id,"
                    ":segment_id,:request_id,:request_id,:response_id,'QUEUED',:summary,:selected_agent,"
                    ":graph_version,:prompt_bundle_version,:registry_version,"
                    ":registry_fingerprint,:trace_id)"
                ),
                {
                    "id": run_id,
                    "user_id": user_id,
                    "conversation_id": conversation["id"],
                    "segment_id": segment_id,
                    "request_id": user_message_id,
                    "response_id": assistant_message_id,
                    "summary": self._safe_summary(submitted_content),
                    "selected_agent": conversation["specialist_user_agent_id"],
                    "graph_version": CURRENT_GRAPH_VERSION,
                    "prompt_bundle_version": CURRENT_PROMPT_BUNDLE_VERSION,
                    "registry_version": TOOL_REGISTRY_VERSION,
                    "registry_fingerprint": DEFAULT_TOOL_REGISTRY.fingerprint,
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
                            "SELECT count(*) FROM messages WHERE conversation_id=:conversation_id "
                            "AND role='USER'"
                        ),
                        {"conversation_id": conversation["id"]},
                    )
                ).scalar_one()
            )
            if user_message_count == 1:
                title_seed = re.sub(r"\s+", " ", submitted_content)[:24]
                if title_seed:
                    title = (
                        f"考公 · {title_seed}"
                        if conversation["specialist_user_agent_id"]
                        else title_seed
                    )
                elif request.attachments:
                    title = "资料对话"
            await connection.execute(
                text(
                    "UPDATE conversations SET title=:title,last_message_at=:now,updated_at=:now "
                    "WHERE id=:id"
                ),
                {"title": title, "now": now, "id": conversation["id"]},
            )
            sequence = await self._append_event(
                connection,
                run_id,
                user_id,
                "run.accepted",
                {"status": "QUEUED", "execution_mode": "START"},
                0,
            )
            result = {
                "user_message_id": user_message_id,
                "run_id": run_id,
                "pending_response_message_id": assistant_message_id,
                "status": "QUEUED",
                "attempt": 0,
            }
        response = self._send_response(conversation, result)
        response["transition"] = transition
        response["run"]["execution_mode"] = "START"  # type: ignore[index]
        response["stream"]["last_sequence"] = sequence  # type: ignore[index]
        if request.context_policy == "ARCHIVE_AND_START" and transition.get(
            "archived_conversation_id"
        ):
            await self._delete_conversation_checkpoints(
                UUID(str(transition["archived_conversation_id"]))
            )
        return response

    async def _delete_conversation_checkpoints(self, conversation_id: UUID) -> None:
        async with self.database.transaction() as connection:
            thread_ids = tuple(
                str(value)
                for value in (
                    await connection.execute(
                        text(
                            "SELECT thread_id FROM conversation_segments "
                            "WHERE conversation_id=:conversation_id"
                        ),
                        {"conversation_id": conversation_id},
                    )
                ).scalars()
            )
            await connection.execute(
                text(
                    "UPDATE conversation_segments SET checkpoint_delete_requested_at=now() "
                    "WHERE conversation_id=:conversation_id"
                ),
                {"conversation_id": conversation_id},
            )
        try:
            async with AsyncPostgresSaver.from_conn_string(self._langgraph_database_url) as saver:
                for thread_id in thread_ids:
                    await saver.adelete_thread(thread_id)
        except Exception:
            return
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE conversation_segments SET checkpoint_deleted_at=now() "
                    "WHERE conversation_id=:conversation_id"
                ),
                {"conversation_id": conversation_id},
            )
