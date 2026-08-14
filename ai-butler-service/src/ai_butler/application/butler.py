"""验证版核心用例服务。

该模块只在短事务中读写 PostgreSQL。模型、对象存储和网络调用由 Worker 或
Adapter 在事务外完成，避免长时间持锁。所有用户资源查询都显式携带 user_id。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.adapters.auth import AuthIdentity
from ai_butler.adapters.documents import SUPPORTED_RAG_MIME_TYPES, chunk_text, extract_text
from ai_butler.adapters.embedding import (
    EmbeddingProvider,
    FakeEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)
from ai_butler.adapters.llm import LLM, FakeLLM, OpenAICompatibleLLM
from ai_butler.adapters.search import (
    FakeSearchProvider,
    SearchError,
    SearchProvider,
    SearchRequest,
    SearchResult,
    SearchUnavailableError,
    TavilySearchProvider,
    minimize_public_query,
)
from ai_butler.adapters.vector import QdrantVectorStore, VectorPoint, VectorStore, VectorStoreError
from ai_butler.agent.availability import (
    AvailabilityInterpretationV1,
    AvailabilityInterpreter,
    quick_availability_options,
)
from ai_butler.agent.evidence import AnswerSegmentV1, EvidenceGate, NumberedEvidence, RagAnswerV1
from ai_butler.agent.runtime import DEFAULT_CAPABILITY_REGISTRY
from ai_butler.api.schemas import (
    ApprovalDecisionRequest,
    AvailabilityRequest,
    CompleteUploadRequest,
    CreateConversationRequest,
    PreferencesRequest,
    ProfileRequest,
    SendMessageRequest,
    TaskExecutionRequest,
    UpdateMeRequest,
    UploadIntentRequest,
)
from ai_butler.config import Settings
from ai_butler.domain.errors import ButlerError, conflict, not_found
from ai_butler.infrastructure.database import AsyncDatabase
from ai_butler.security import (
    issue_access_token,
    issue_refresh_token,
    issue_signed_ticket,
    refresh_token_session_id,
    token_hmac,
)

AGENT_NAMESPACE = UUID("d2542f33-9752-4d8a-bbdc-c77ecf2591d4")
BUTLER_DEFINITION_ID = uuid5(AGENT_NAMESPACE, "BUTLER:1")
CIVIL_DEFINITION_ID = uuid5(AGENT_NAMESPACE, "CIVIL_SERVICE_EXAM:1")
IELTS_DEFINITION_ID = uuid5(AGENT_NAMESPACE, "IELTS:1")
JOB_SEARCH_DEFINITION_ID = uuid5(AGENT_NAMESPACE, "JOB_SEARCH:1")
PUBLIC_SOURCE_ID = uuid5(AGENT_NAMESPACE, "SYNTHETIC_PUBLIC_SOURCE")
PUBLIC_CHUNK_ID = uuid5(AGENT_NAMESPACE, "SYNTHETIC_PUBLIC_SOURCE:0")
PLAN_PATTERN = re.compile(r"国考|省考|公务员|行测|申论|备考|计划")
PLAN_ACTION_PATTERN = re.compile(r"制定|生成|安排|调整|减少|增加|计划")
# 这里只做“是否值得进入结构化提取”的宽松检测；具体分钟与日期仍由 LLM 候选和
# AvailabilityInterpreter 的确定性规则共同完成，不能把正则命中当成业务事实。
TIME_PATTERN = re.compile(r"\d+\s*个?\s*(?:小时|分钟)|每天|每周|工作日|周末|周[一二三四五六日天]")
SEARCH_PATTERN = re.compile(
    r"政策|公告|报名|考试时间|岗位|大纲|资料|教材|联网|搜索|查询|最新|今年|202\d"
)
WEB_FORCE_PATTERN = re.compile(r"政策|公告|报名|考试时间|岗位|大纲|联网|搜索|查询|最新|今年|202\d")
PRIVATE_SEARCH_PATTERN = re.compile(r"我的资料|附件|文件|文档")
NON_TERMINAL_RUN_SQL = (
    "'QUEUED','RUNNING','AWAITING_INPUT','AWAITING_APPROVAL','FAILED_RETRYABLE','CANCEL_REQUESTED'"
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _row(result: Any) -> dict[str, Any] | None:
    mapping = result.mappings().first()
    return dict(mapping) if mapping is not None else None


def _encode_cursor(*values: object) -> str:
    payload = _json([str(value) for value in values]).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str, expected_parts: int) -> list[str]:
    """解析客户端不透明游标；格式或字段数错误统一映射为安全业务错误。"""

    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ButlerError("INVALID_CURSOR", "分页游标无效", 400) from exc
    if (
        not isinstance(value, list)
        or len(value) != expected_parts
        or not all(isinstance(item, str) for item in value)
    ):
        raise ButlerError("INVALID_CURSOR", "分页游标无效", 400)
    return value


def _message_request_hash(request: SendMessageRequest) -> str:
    """计算会话内消息幂等哈希，附件按展示位置排序后进入摘要。"""

    canonical = {
        "content": request.content.strip(),
        "attachments": sorted(
            ((item.position, str(item.file_id)) for item in request.attachments),
            key=lambda item: item[0],
        ),
        "selection": request.selection.model_dump(mode="json") if request.selection else None,
    }
    return hashlib.sha256(_json(canonical).encode()).hexdigest()


class ButlerService:
    """编排认证、聊天、计划、任务和验证版 Agent 的事务。"""

    def __init__(
        self,
        database: AsyncDatabase,
        settings: Settings,
        search_provider: SearchProvider | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
        llm: LLM | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.search_provider = search_provider or self._build_search_provider(settings)
        self.embedding_provider = embedding_provider or self._build_embedding_provider(settings)
        self.vector_store = vector_store or QdrantVectorStore(
            settings.qdrant_url,
            settings.qdrant_collection,
            settings.embedding_dimensions,
        )
        self.availability_interpreter = AvailabilityInterpreter(llm or self._build_llm(settings))
        self.evidence_gate = EvidenceGate(tuple(settings.official_source_domains))

    async def login(
        self,
        identity: AuthIdentity,
        device_id: str,
    ) -> dict[str, object]:
        """幂等登录并初始化唯一 BUTLER 主聊天。"""

        now = datetime.now(UTC)
        async with self.database.transaction() as connection:
            await self._ensure_agent_definitions(connection)
            existing = _row(
                await connection.execute(
                    text(
                        "SELECT u.* FROM users u JOIN user_identities i ON i.user_id=u.id "
                        "WHERE i.provider=:provider AND i.provider_subject=:subject FOR UPDATE"
                    ),
                    {"provider": identity.provider, "subject": identity.subject},
                )
            )
            is_new = existing is None
            user_id = identity.user_id if existing is None else UUID(str(existing["id"]))
            if existing is None:
                await connection.execute(
                    text(
                        "INSERT INTO users(id,status,nickname,locale,timezone) "
                        "VALUES(:id,'ACTIVE','微信用户','zh-CN','Asia/Shanghai')"
                    ),
                    {"id": user_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO user_identities(id,user_id,provider,provider_subject,last_login_at) "
                        "VALUES(:id,:user_id,:provider,:subject,:now)"
                    ),
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "provider": identity.provider,
                        "subject": identity.subject,
                        "now": now,
                    },
                )
                await connection.execute(
                    text("INSERT INTO user_profiles(user_id) VALUES(:user_id)"),
                    {"user_id": user_id},
                )
                await connection.execute(
                    text("INSERT INTO memory_policy_state(user_id) VALUES(:user_id)"),
                    {"user_id": user_id},
                )
            else:
                if existing["status"] != "ACTIVE":
                    raise ButlerError("ACCOUNT_UNAVAILABLE", "账号当前不可登录", 403)
                await connection.execute(
                    text(
                        "UPDATE user_identities SET last_login_at=:now "
                        "WHERE provider=:provider AND provider_subject=:subject"
                    ),
                    {"now": now, "provider": identity.provider, "subject": identity.subject},
                )
            await self._ensure_user_workspace(connection, user_id)
            session_id = uuid4()
            refresh_token = issue_refresh_token(session_id)
            await connection.execute(
                text(
                    "INSERT INTO auth_sessions(id,user_id,refresh_token_hash,device_id,status,expires_at) "
                    "VALUES(:id,:user_id,:token_hash,:device_id,'ACTIVE',:expires_at)"
                ),
                {
                    "id": session_id,
                    "user_id": user_id,
                    "token_hash": token_hmac(
                        refresh_token, self.settings.auth_refresh_token_secret
                    ),
                    "device_id": device_id,
                    "expires_at": now + timedelta(seconds=self.settings.auth_refresh_token_seconds),
                },
            )
            user = await self._get_user(connection, user_id)
        return self._token_response(user, session_id, refresh_token, is_new)

    async def refresh(self, refresh_token: str, device_id: str) -> dict[str, object]:
        """原子轮换刷新令牌；旧令牌复用会撤销当前会话。"""

        session_id = refresh_token_session_id(refresh_token)
        supplied_hash = token_hmac(refresh_token, self.settings.auth_refresh_token_secret)
        now = datetime.now(UTC)
        async with self.database.transaction() as connection:
            session = _row(
                await connection.execute(
                    text("SELECT * FROM auth_sessions WHERE id=:id FOR UPDATE"),
                    {"id": session_id},
                )
            )
            if session is None or session["status"] != "ACTIVE":
                raise ButlerError("INVALID_REFRESH_TOKEN", "登录状态已失效", 401)
            if session["device_id"] not in (None, device_id):
                raise ButlerError("INVALID_REFRESH_TOKEN", "登录状态已失效", 401)
            if session["expires_at"] <= now:
                await connection.execute(
                    text("UPDATE auth_sessions SET status='EXPIRED' WHERE id=:id"),
                    {"id": session_id},
                )
                raise ButlerError("REFRESH_TOKEN_EXPIRED", "登录状态已过期", 401)
            if session["refresh_token_hash"] != supplied_hash:
                await connection.execute(
                    text("UPDATE auth_sessions SET status='REVOKED',revoked_at=:now WHERE id=:id"),
                    {"id": session_id, "now": now},
                )
                raise ButlerError("REFRESH_TOKEN_REUSED", "检测到旧令牌复用，请重新登录", 401)
            rotated = issue_refresh_token(session_id)
            await connection.execute(
                text(
                    "UPDATE auth_sessions SET refresh_token_hash=:token_hash,last_used_at=:now "
                    "WHERE id=:id"
                ),
                {
                    "id": session_id,
                    "token_hash": token_hmac(rotated, self.settings.auth_refresh_token_secret),
                    "now": now,
                },
            )
            user = await self._get_user(connection, UUID(str(session["user_id"])))
        return self._token_response(user, session_id, rotated, False)

    async def logout(self, user_id: UUID, refresh_token: str) -> None:
        session_id = refresh_token_session_id(refresh_token)
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE auth_sessions SET status='REVOKED',revoked_at=now() "
                    "WHERE id=:id AND user_id=:user_id AND status='ACTIVE'"
                ),
                {"id": session_id, "user_id": user_id},
            )

    async def get_me(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            return await self._get_user(connection, user_id)

    async def update_me(self, user_id: UUID, request: UpdateMeRequest) -> dict[str, object]:
        values = request.model_dump(exclude_unset=True)
        if not values:
            return await self.get_me(user_id)
        allowed = {"nickname", "locale", "timezone"}
        assignments = [f"{key}=:{key}" for key in values if key in allowed]
        if request.avatar_file_id is not None:
            raise ButlerError("AVATAR_NOT_READY", "头像文件尚未完成验证", 409)
        async with self.database.transaction() as connection:
            if assignments:
                await connection.execute(
                    text(
                        f"UPDATE users SET {','.join(assignments)},updated_at=now() "  # noqa: S608
                        "WHERE id=:user_id AND status='ACTIVE'"
                    ),
                    {**{key: values[key] for key in values if key in allowed}, "user_id": user_id},
                )
            return await self._get_user(connection, user_id)

    async def get_profile(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text("SELECT * FROM user_profiles WHERE user_id=:user_id"),
                    {"user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            return row

    async def put_profile(self, user_id: UUID, request: ProfileRequest) -> dict[str, object]:
        async with self.database.transaction() as connection:
            result = await connection.execute(
                text(
                    "UPDATE user_profiles SET education_level=:education_level,major=:major,"
                    "region_code=:region_code,current_level=:current_level,"
                    "existing_materials=CAST(:materials AS jsonb),profile_version=profile_version+1,updated_at=now() "
                    "WHERE user_id=:user_id AND profile_version=:expected_version RETURNING *"
                ),
                {
                    "user_id": user_id,
                    "expected_version": request.expected_version,
                    "education_level": request.education_level,
                    "major": request.major,
                    "region_code": request.region_code,
                    "current_level": request.current_level,
                    "materials": _json([str(item) for item in request.existing_material_file_ids]),
                },
            )
            row = _row(result)
            if row is None:
                raise conflict("RESOURCE_VERSION_CONFLICT", "画像版本已更新，请刷新后重试")
            return row

    async def get_availability(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM study_availability WHERE user_id=:user_id "
                            "ORDER BY day_of_week NULLS FIRST,start_time NULLS FIRST"
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
            profile = _row(
                await connection.execute(
                    text("SELECT profile_version FROM user_profiles WHERE user_id=:user_id"),
                    {"user_id": user_id},
                )
            )
            return {
                "version": profile["profile_version"] if profile else 1,
                "windows": [dict(row) for row in rows],
            }

    async def put_availability(
        self, user_id: UUID, request: AvailabilityRequest
    ) -> dict[str, object]:
        self._validate_availability_overlap(request)
        async with self.database.transaction() as connection:
            locked = _row(
                await connection.execute(
                    text(
                        "SELECT profile_version FROM user_profiles WHERE user_id=:user_id FOR UPDATE"
                    ),
                    {"user_id": user_id},
                )
            )
            if locked is None or locked["profile_version"] != request.expected_version:
                raise conflict("RESOURCE_VERSION_CONFLICT", "学习时间版本已更新，请刷新后重试")
            await connection.execute(
                text("DELETE FROM study_availability WHERE user_id=:user_id"),
                {"user_id": user_id},
            )
            for window in request.windows:
                await connection.execute(
                    text(
                        "INSERT INTO study_availability(id,user_id,day_of_week,start_time,end_time,"
                        "available_minutes,effective_from,effective_to) "
                        "VALUES(:id,:user_id,:day,:start,:end,:minutes,:effective_from,:effective_to)"
                    ),
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "day": window.day_of_week,
                        "start": window.start_time,
                        "end": window.end_time,
                        "minutes": window.available_minutes,
                        "effective_from": window.effective_from,
                        "effective_to": window.effective_to,
                    },
                )
            await connection.execute(
                text(
                    "UPDATE user_profiles SET profile_version=profile_version+1,updated_at=now() "
                    "WHERE user_id=:user_id"
                ),
                {"user_id": user_id},
            )
        return await self.get_availability(user_id)

    async def get_preferences(self, user_id: UUID) -> dict[str, object]:
        profile = await self.get_profile(user_id)
        reminder = profile["notification_preferences"] or {
            "enabled": True,
            "channels": ["IN_APP"],
            "advance_minutes": 15,
        }
        return {
            "version": profile["profile_version"],
            "task_reminder": reminder,
            "plan_change_confirmation_required": True,
            "read_only_policies": ["plan_change_confirmation_required"],
        }

    async def patch_preferences(
        self, user_id: UUID, request: PreferencesRequest
    ) -> dict[str, object]:
        async with self.database.transaction() as connection:
            updated = await connection.execute(
                text(
                    "UPDATE user_profiles SET notification_preferences=CAST(:settings AS jsonb),"
                    "profile_version=profile_version+1,updated_at=now() "
                    "WHERE user_id=:user_id AND profile_version=:version RETURNING user_id"
                ),
                {
                    "settings": request.task_reminder.model_dump_json(),
                    "user_id": user_id,
                    "version": request.expected_version,
                },
            )
            if updated.first() is None:
                raise conflict("RESOURCE_VERSION_CONFLICT", "设置版本已更新，请刷新后重试")
        return await self.get_preferences(user_id)

    async def delete_account(self, user_id: UUID) -> dict[str, object]:
        now = datetime.now(UTC)
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE users SET status='DELETING',updated_at=:now WHERE id=:user_id "
                    "AND status='ACTIVE'"
                ),
                {"user_id": user_id, "now": now},
            )
            await connection.execute(
                text(
                    "UPDATE auth_sessions SET status='REVOKED',revoked_at=:now "
                    "WHERE user_id=:user_id AND status='ACTIVE'"
                ),
                {"user_id": user_id, "now": now},
            )
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='CANCEL_REQUESTED',cancel_requested_at=:now "
                    "WHERE user_id=:user_id AND status IN ('QUEUED','RUNNING','AWAITING_INPUT','AWAITING_APPROVAL')"
                ),
                {"user_id": user_id, "now": now},
            )
        return {"status": "DELETING", "accepted_at": now}

    async def list_agent_definitions(self) -> dict[str, object]:
        """返回用户可见的专业入口目录，不暴露内部定义或用户 Agent ID。"""

        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT code,name,COALESCE(description,'') AS description,catalog_status,"
                            "catalog_metadata FROM agent_definitions WHERE catalog_status <> 'HIDDEN' "
                            "ORDER BY display_order,code"
                        )
                    )
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows:
            metadata = row["catalog_metadata"] if isinstance(row["catalog_metadata"], dict) else {}
            items.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "description": row["description"],
                    "icon": str(metadata.get("icon", "AI")),
                    "availability": row["catalog_status"],
                    "welcome_message": str(metadata.get("welcome_message", "")),
                    "starter_prompts": metadata.get("starter_prompts", []),
                }
            )
        return {"items": items}

    async def list_conversations(
        self, user_id: UUID, limit: int = 30, cursor: str | None = None
    ) -> dict[str, object]:
        """按当前优先、最近活动倒序列出用户可见会话。"""

        parameters: dict[str, object] = {"user_id": user_id, "limit": limit + 1}
        cursor_clause = ""
        if cursor:
            rank, timestamp, conversation_id = _decode_cursor(cursor, 3)
            try:
                parameters.update(
                    {
                        "cursor_rank": int(rank),
                        "cursor_time": datetime.fromisoformat(timestamp),
                        "cursor_id": UUID(conversation_id),
                    }
                )
            except ValueError as exc:
                raise ButlerError("INVALID_CURSOR", "分页游标无效", 400) from exc
            cursor_clause = (
                "AND (CASE WHEN c.status='CURRENT' THEN 0 ELSE 1 END > :cursor_rank OR "
                "(CASE WHEN c.status='CURRENT' THEN 0 ELSE 1 END = :cursor_rank AND "
                "(COALESCE(c.last_message_at,c.created_at) < :cursor_time OR "
                "(COALESCE(c.last_message_at,c.created_at) = :cursor_time "
                "AND c.id < :cursor_id)))) "
            )
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT c.*,ad.code AS specialist_code,ad.name AS specialist_name,"  # noqa: S608
                            "ad.catalog_metadata AS specialist_metadata,r.id AS active_run_id,"
                            "r.status AS active_run_status,lm.content AS last_message_content,"
                            "lm.created_at AS last_message_created_at "
                            "FROM conversations c "
                            "LEFT JOIN user_agents sua ON sua.id=c.specialist_user_agent_id "
                            "LEFT JOIN agent_definitions ad ON ad.id=sua.agent_definition_id "
                            f"LEFT JOIN agent_runs r ON r.conversation_id=c.id AND r.status IN ({NON_TERMINAL_RUN_SQL}) "
                            "LEFT JOIN LATERAL (SELECT content,created_at FROM messages "
                            "WHERE conversation_id=c.id AND role IN ('USER','ASSISTANT') "
                            "ORDER BY created_at DESC,id DESC LIMIT 1) lm ON true "
                            f"WHERE c.user_id=:user_id {cursor_clause}"
                            "ORDER BY CASE WHEN c.status='CURRENT' THEN 0 ELSE 1 END,"
                            "COALESCE(c.last_message_at,c.created_at) DESC,c.id DESC LIMIT :limit"
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        items = [self._conversation_response(dict(row)) for row in visible_rows]
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = _encode_cursor(
                0 if last["status"] == "CURRENT" else 1,
                last["last_message_at"] or last["created_at"],
                last["id"],
            )
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    async def create_conversation(
        self, user_id: UUID, request: CreateConversationRequest
    ) -> dict[str, object]:
        """幂等创建会话并原子归档当前会话；外部等待不在事务内执行。"""

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
                        "SELECT c.*,ad.code AS specialist_code,ad.name AS specialist_name,"
                        "ad.catalog_metadata AS specialist_metadata,NULL::uuid AS active_run_id,"
                        "NULL::varchar AS active_run_status,NULL::text AS last_message_content,"
                        "NULL::timestamptz AS last_message_created_at FROM conversations c "
                        "LEFT JOIN user_agents ua ON ua.id=c.specialist_user_agent_id "
                        "LEFT JOIN agent_definitions ad ON ad.id=ua.agent_definition_id "
                        "WHERE c.user_id=:user_id AND c.client_conversation_id=:client_id"
                    ),
                    {"user_id": user_id, "client_id": request.client_conversation_id},
                )
            )
            if duplicate:
                if duplicate["specialist_code"] != request.specialist_code:
                    raise conflict(
                        "IDEMPOTENCY_KEY_REUSED",
                        "会话标识已用于不同的专业助理，请重新创建",
                    )
                return self._conversation_response(duplicate)
            await self._lock_user_and_check_global_run(connection, user_id)
            specialist = await self._resolve_specialist(
                connection, user_id, request.specialist_code
            )
            current = _row(
                await connection.execute(
                    text(
                        "SELECT id FROM conversations WHERE user_id=:user_id AND status='CURRENT' FOR UPDATE"
                    ),
                    {"user_id": user_id},
                )
            )
            if current:
                await connection.execute(
                    text(
                        "UPDATE conversations SET status='ARCHIVED',archived_at=:now,updated_at=:now WHERE id=:id"
                    ),
                    {"id": current["id"], "now": now},
                )
            conversation_id = uuid4()
            segment_id = uuid4()
            title = f"{specialist['name']}助理" if specialist else "新的对话"
            await connection.execute(
                text(
                    "INSERT INTO conversations(id,user_id,user_agent_id,active_segment_id,"
                    "client_conversation_id,title,status,specialist_user_agent_id) "
                    "VALUES(:id,:user_id,:butler,NULL,:client_id,:title,'CURRENT',:specialist)"
                ),
                {
                    "id": conversation_id,
                    "user_id": user_id,
                    "butler": uuid5(user_id, "BUTLER"),
                    "client_id": request.client_conversation_id,
                    "title": title,
                    "specialist": specialist["user_agent_id"] if specialist else None,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO conversation_segments(id,conversation_id,user_id,sequence,thread_id,status) "
                    "VALUES(:id,:conversation,:user_id,1,:thread,'ACTIVE')"
                ),
                {
                    "id": segment_id,
                    "conversation": conversation_id,
                    "user_id": user_id,
                    "thread": f"thread-{uuid4()}",
                },
            )
            await connection.execute(
                text("UPDATE conversations SET active_segment_id=:segment WHERE id=:id"),
                {"segment": segment_id, "id": conversation_id},
            )
            last_message = None
            if specialist:
                message_id = uuid4()
                welcome_message = specialist["welcome_message"]
                await connection.execute(
                    text(
                        "INSERT INTO messages(id,user_id,conversation_id,segment_id,role,status,content) "
                        "VALUES(:id,:user_id,:conversation,:segment,'ASSISTANT','COMPLETED',:content)"
                    ),
                    {
                        "id": message_id,
                        "user_id": user_id,
                        "conversation": conversation_id,
                        "segment": segment_id,
                        "content": welcome_message,
                    },
                )
                await connection.execute(
                    text(
                        "UPDATE conversations SET last_message_at=:now,updated_at=:now WHERE id=:id"
                    ),
                    {"now": now, "id": conversation_id},
                )
                last_message = {"content": welcome_message, "created_at": now}
            return {
                "id": conversation_id,
                "title": title,
                "status": "CURRENT",
                "specialist": self._specialist_response(specialist),
                "last_message": last_message,
                "last_message_at": now if last_message else None,
                "active_run": None,
                "created_at": now,
                "updated_at": now,
            }

    async def get_conversation(self, user_id: UUID, conversation_id: UUID) -> dict[str, object]:
        """读取一个归属当前用户的会话，跨用户访问按不存在处理。"""

        async with self.database.connect() as connection:
            row = await self._conversation_row(connection, user_id, conversation_id)
        if row is None:
            raise not_found()
        return self._conversation_response(row)

    async def list_messages(
        self,
        user_id: UUID,
        conversation_id: UUID,
        limit: int = 30,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """按会话分页读取消息；响应保持正序以供聊天时间线直接追加。"""

        parameters: dict[str, object] = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "limit": limit + 1,
        }
        cursor_clause = ""
        if cursor:
            timestamp, message_id = _decode_cursor(cursor, 2)
            try:
                parameters.update(
                    {
                        "cursor_time": datetime.fromisoformat(timestamp),
                        "cursor_id": UUID(message_id),
                    }
                )
            except ValueError as exc:
                raise ButlerError("INVALID_CURSOR", "分页游标无效", 400) from exc
            cursor_clause = "AND (created_at,id) < (:cursor_time,:cursor_id) "
        async with self.database.connect() as connection:
            owned = _row(
                await connection.execute(
                    text(
                        "SELECT id FROM conversations WHERE id=:conversation_id AND user_id=:user_id"
                    ),
                    parameters,
                )
            )
            if owned is None:
                raise not_found()
            result = await connection.execute(
                text(
                    "SELECT id,role,status,content,structured_content AS cards,agent_run_id,created_at "  # noqa: S608
                    "FROM messages WHERE user_id=:user_id AND conversation_id=:conversation_id "
                    "AND role IN ('USER','ASSISTANT','SYSTEM_EVENT') "
                    f"{cursor_clause}ORDER BY created_at DESC,id DESC LIMIT :limit"
                ),
                parameters,
            )
            rows = [dict(row) for row in result.mappings().all()]
            await self._hydrate_approval_cards(connection, user_id, rows)
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        items = list(reversed(visible_rows))
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = _encode_cursor(last["created_at"], last["id"])
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    async def send_message(
        self, user_id: UUID, conversation_id: UUID, request: SendMessageRequest
    ) -> dict[str, object]:
        """提交消息或恢复等待输入的 run；事务提交前不返回。"""

        now = datetime.now(UTC)
        async with self.database.transaction() as connection:
            await connection.execute(
                text("SELECT id FROM users WHERE id=:user_id FOR UPDATE"), {"user_id": user_id}
            )
            conversation = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM conversations WHERE id=:conversation_id AND user_id=:user_id FOR UPDATE"
                    ),
                    {"conversation_id": conversation_id, "user_id": user_id},
                )
            )
            if conversation is None:
                raise not_found()
            duplicate = _row(
                await connection.execute(
                    text(
                        "SELECT m.id AS user_message_id,m.client_request_hash,r.id AS run_id,"
                        "r.response_message_id,r.status,r.attempt "
                        "FROM messages m JOIN agent_runs r ON r.request_message_id=m.id "
                        "WHERE m.conversation_id=:conversation_id AND m.client_message_id=:client_id"
                    ),
                    {
                        "conversation_id": conversation_id,
                        "client_id": request.client_message_id,
                    },
                )
            )
            if duplicate:
                if duplicate.pop("client_request_hash") != _message_request_hash(request):
                    raise conflict("IDEMPOTENCY_KEY_REUSED", "消息标识已用于不同内容，请重新发送")
                return self._send_response(conversation, duplicate)
            global_active = _row(
                await connection.execute(
                    text(
                        f"SELECT * FROM agent_runs WHERE user_id=:user_id AND status IN ({NON_TERMINAL_RUN_SQL}) "  # noqa: S608
                        "FOR UPDATE"
                    ),
                    {"user_id": user_id},
                )
            )
            if global_active and global_active["conversation_id"] != conversation_id:
                raise ButlerError(
                    "GLOBAL_RUN_IN_PROGRESS",
                    "另一个对话正在处理中",
                    409,
                    details={
                        "run_id": str(global_active["id"]),
                        "conversation_id": str(global_active["conversation_id"]),
                    },
                )
            active = global_active
            if active and active["status"] != "AWAITING_INPUT":
                error_by_status = {
                    "AWAITING_APPROVAL": ("APPROVAL_REQUIRED", "请使用计划卡片完成审批"),
                    "FAILED_RETRYABLE": ("RUN_RETRY_REQUIRED", "请先重试或取消当前运行"),
                }
                code, message = error_by_status.get(
                    active["status"], ("CONVERSATION_BUSY", "管家正在处理上一条消息")
                )
                raise conflict(code, message)
            if conversation["status"] == "ARCHIVED":
                await connection.execute(
                    text(
                        "UPDATE conversations SET status='ARCHIVED',archived_at=:now,updated_at=:now "
                        "WHERE user_id=:user_id AND status='CURRENT' AND id<>:id"
                    ),
                    {"user_id": user_id, "id": conversation_id, "now": now},
                )
                await connection.execute(
                    text(
                        "UPDATE conversations SET status='CURRENT',archived_at=NULL,updated_at=:now WHERE id=:id"
                    ),
                    {"id": conversation_id, "now": now},
                )
                conversation["status"] = "CURRENT"
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
                        {"id": active["response_message_id"], "user_id": user_id},
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
                        "id": active["response_message_id"],
                        "user_id": user_id,
                    },
                )
            elif active and submitted_content:
                prompt = _row(
                    await connection.execute(
                        text(
                            "SELECT structured_content FROM messages WHERE id=:id AND user_id=:user_id"
                        ),
                        {"id": active["response_message_id"], "user_id": user_id},
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
                            "id": active["response_message_id"],
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
                        "UPDATE agent_runs SET request_message_id=:message_id,response_message_id=:response_id,"
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
                    "client_request_hash,role,status,content,structured_content) "
                    "VALUES(:id,:user_id,:conversation_id,:segment_id,:run_id,:client_id,:request_hash,"
                    "'USER','COMPLETED',:content,CAST(:structured AS jsonb))"
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
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO messages(id,user_id,conversation_id,segment_id,agent_run_id,role,status,content) "
                    "VALUES(:id,:user_id,:conversation_id,:segment_id,:run_id,'ASSISTANT','PENDING','')"
                ),
                {
                    "id": assistant_message_id,
                    "user_id": user_id,
                    "conversation_id": conversation["id"],
                    "segment_id": segment_id,
                    "run_id": run_id,
                },
            )
            if not active:
                await connection.execute(
                    text(
                        "INSERT INTO agent_runs(id,user_id,conversation_id,segment_id,request_message_id,"
                        "response_message_id,status,pending_action,pending_action_key,input_summary,"
                        "selected_user_agent_id) "
                        "VALUES(:id,:user_id,:conversation_id,:segment_id,:request_id,:response_id,'QUEUED',"
                        "'START',:action_key,:summary,:selected_agent)"
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
                        "INSERT INTO message_attachments(message_id,file_id,position) VALUES(:message,:file,:position)"
                    ),
                    {
                        "message": user_message_id,
                        "file": attachment.file_id,
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
                "response_message_id": assistant_message_id,
                "status": "QUEUED",
                "attempt": int(active["attempt"]) if active else 0,
            }
        response = self._send_response(conversation, result)
        response["run"]["execution_mode"] = execution_mode  # type: ignore[index]
        response["stream"]["last_sequence"] = sequence  # type: ignore[index]
        return response

    async def get_run(self, user_id: UUID, run_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id AND user_id=:user_id"),
                    {"id": run_id, "user_id": user_id},
                )
            )
            if run is None:
                raise not_found()
            approval = _row(
                await connection.execute(
                    text(
                        "SELECT id,approval_version FROM approval_decisions "
                        "WHERE agent_run_id=:run_id AND status='PENDING' ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"run_id": run_id},
                )
            )
            return {
                "schema_version": "2.0",
                "run_id": run_id,
                "status": run["status"],
                "attempt": run["attempt"],
                "last_sequence": run["last_event_sequence"],
                "response_message": {"id": run["response_message_id"]},
                "data": {},
                "citations": [],
                "warnings": [],
                "next_action": (
                    {
                        "type": "REVIEW_PLAN",
                        "approval_id": approval["id"],
                        "approval_version": approval["approval_version"],
                    }
                    if approval
                    else None
                ),
                "error": (
                    {"code": run["error_code"], "retryable": run["status"] == "FAILED_RETRYABLE"}
                    if run["error_code"]
                    else None
                ),
                "created_at": run["created_at"],
                "updated_at": run["updated_at"],
            }

    async def stream_ticket(self, user_id: UUID, run_id: UUID) -> dict[str, object]:
        run = await self.get_run(user_id, run_id)
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.stream_ticket_seconds)
        return {
            "events_url": f"/v1/agent-runs/{run_id}/events",
            "ticket": issue_signed_ticket(
                run_id, self.settings.stream_ticket_secret, self.settings.stream_ticket_seconds
            ),
            "expires_at": expires_at,
            "last_sequence": run["last_sequence"],
        }

    async def list_events(self, user_id: UUID, run_id: UUID, after: int) -> list[dict[str, object]]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT event_type,sequence,attempt,payload,created_at FROM agent_run_events "
                            "WHERE run_id=:run_id AND user_id=:user_id AND sequence>:after ORDER BY sequence LIMIT 100"
                        ),
                        {"run_id": run_id, "user_id": user_id, "after": after},
                    )
                )
                .mappings()
                .all()
            )
            return [dict(item) for item in rows]

    async def event_owner(self, run_id: UUID) -> UUID:
        """返回流票据对应 run 的所有者；仅在票据验证后用于隔离查询。"""

        async with self.database.connect() as connection:
            value = (
                await connection.execute(
                    text("SELECT user_id FROM agent_runs WHERE id=:id"), {"id": run_id}
                )
            ).scalar_one_or_none()
            if value is None:
                raise not_found()
            return UUID(str(value))

    async def cancel_run(self, user_id: UUID, run_id: UUID) -> dict[str, object]:
        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id AND user_id=:user_id FOR UPDATE"),
                    {"id": run_id, "user_id": user_id},
                )
            )
            if run is None:
                raise not_found()
            if run["status"] in {"SUCCEEDED", "FAILED_FINAL", "CANCELLED"}:
                return {"run_id": run_id, "status": run["status"]}
            status = "CANCEL_REQUESTED" if run["status"] == "RUNNING" else "CANCELLED"
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status=:status,cancel_requested_at=now(),updated_at=now() WHERE id=:id"
                ),
                {"status": status, "id": run_id},
            )
            if status == "CANCELLED":
                await connection.execute(
                    text("UPDATE messages SET status='CANCELLED' WHERE id=:id"),
                    {"id": run["response_message_id"]},
                )
                await self._append_event(
                    connection, run_id, user_id, "run.cancelled", {}, run["attempt"]
                )
            return {"run_id": run_id, "status": status}

    async def retry_run(
        self, user_id: UUID, run_id: UUID, expected_attempt: int
    ) -> dict[str, object]:
        async with self.database.transaction() as connection:
            result = await connection.execute(
                text(
                    "UPDATE agent_runs SET status='QUEUED',pending_action='RETRY',attempt=attempt+1,"
                    "pending_action_key=:key,error_code=NULL,updated_at=now() "
                    "WHERE id=:id AND user_id=:user_id AND status='FAILED_RETRYABLE' AND attempt=:attempt RETURNING attempt"
                ),
                {
                    "id": run_id,
                    "user_id": user_id,
                    "attempt": expected_attempt,
                    "key": f"retry:{run_id}:{expected_attempt + 1}",
                },
            )
            row = result.first()
            if row is None:
                raise conflict("RUN_RETRY_CONFLICT", "运行状态或尝试次数已更新")
            await self._append_event(connection, run_id, user_id, "message.reset", {}, row[0])
            return {"run_id": run_id, "status": "QUEUED", "attempt": row[0]}

    async def decide_approval(
        self, user_id: UUID, approval_id: UUID, request: ApprovalDecisionRequest
    ) -> dict[str, object]:
        if approval_id != request.approval_id:
            raise ButlerError("APPROVAL_ID_MISMATCH", "审批标识不匹配", 400)
        async with self.database.transaction() as connection:
            approval = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM approval_decisions WHERE id=:id AND user_id=:user_id FOR UPDATE"
                    ),
                    {"id": approval_id, "user_id": user_id},
                )
            )
            if approval is None:
                raise not_found()
            if approval["status"] != "PENDING":
                return {"approval_id": approval_id, "status": approval["status"]}
            if approval["approval_version"] != request.expected_approval_version:
                raise ButlerError(
                    "APPROVAL_VERSION_CONFLICT",
                    "审批版本已更新，请刷新后重试",
                    409,
                    details={
                        "approval_id": str(approval_id),
                        "current_approval_version": int(approval["approval_version"]),
                    },
                )
            items = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM approval_decision_items WHERE approval_id=:id FOR UPDATE"
                        ),
                        {"id": approval_id},
                    )
                )
                .mappings()
                .all()
            )
            if request.action == "APPROVE":
                for item in items:
                    plan = _row(
                        await connection.execute(
                            text("SELECT current_revision_id FROM plans WHERE id=:id FOR UPDATE"),
                            {"id": item["plan_id"]},
                        )
                    )
                    expected = item["expected_current_revision_id"]
                    if plan is None or plan["current_revision_id"] != expected:
                        raise conflict("PLAN_REVISION_CONFLICT", "计划版本已更新，请重新生成草案")
                for item in items:
                    await self._publish_revision(connection, user_id, dict(item))
            terminal = "APPROVED" if request.action == "APPROVE" else request.action + "ED"
            await connection.execute(
                text(
                    "UPDATE approval_decisions SET status=:status,action=:action,feedback=:feedback,decided_at=now() "
                    "WHERE id=:id"
                ),
                {
                    "status": terminal,
                    "action": request.action,
                    "feedback": request.feedback,
                    "id": approval_id,
                },
            )
            run_id = UUID(str(approval["agent_run_id"]))
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='QUEUED',pending_action='APPROVAL_RESUME',"
                    "pending_action_key=:key,updated_at=now() WHERE id=:run_id"
                ),
                {
                    "key": f"approval:{approval_id}:{request.expected_approval_version}",
                    "run_id": run_id,
                },
            )
            await self._append_event(
                connection,
                run_id,
                user_id,
                "run.status",
                {"status": "QUEUED", "approval_action": request.action},
                0,
            )
            return {"approval_id": approval_id, "status": terminal, "run_id": run_id}

    async def dashboard(self, user_id: UUID, requested_date: date) -> dict[str, object]:
        plans = await self.list_plans(user_id)
        tasks = await self.list_tasks(user_id, requested_date, requested_date)
        plan_items = cast(list[dict[str, Any]], plans["items"])
        task_items = cast(list[dict[str, Any]], tasks["items"])
        done = sum(1 for task in task_items if task["status"] == "DONE")
        return {
            "date": requested_date,
            "timezone": (await self.get_me(user_id))["timezone"],
            "experience_state": "ACTIVE" if plan_items else "EMPTY",
            "butler": {
                "status": "ONLINE",
                "active_specialist_count": 1,
                "summary": f"今天有 {len(task_items) - done} 项任务待完成",
            },
            "plan_summary": {
                "total": len(plan_items),
                "active": sum(1 for plan in plan_items if plan["status"] == "ACTIVE"),
                "completed": sum(1 for plan in plan_items if plan["status"] == "COMPLETED"),
            },
            "task_summary": {
                "today_total": len(task_items),
                "today_done": done,
                "week_total": len(task_items),
                "week_done": done,
                "overloaded_minutes": 0,
            },
            "plans": plan_items,
            "today_tasks": task_items,
        }

    async def list_plans(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT p.id,p.goal_id,p.title,p.status,p.current_revision_id,r.start_date,r.end_date,"
                            "r.weekly_minutes,p.updated_at,g.goal_type,"
                            "COUNT(t.id) AS task_total,COUNT(t.id) FILTER(WHERE t.status='DONE') AS task_done "
                            "FROM plans p JOIN goals g ON g.id=p.goal_id "
                            "LEFT JOIN plan_revisions r ON r.id=p.current_revision_id "
                            "LEFT JOIN tasks t ON t.plan_id=p.id WHERE p.user_id=:user_id "
                            "GROUP BY p.id,r.id,g.goal_type ORDER BY p.updated_at DESC"
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
            items = []
            for row in rows:
                item = dict(row)
                total = int(item.pop("task_total"))
                completed = int(item.pop("task_done"))
                item["progress"] = {
                    "completed": completed,
                    "total": total,
                    "percent": round(completed * 100 / total) if total else 0,
                }
                items.append(item)
            return {"items": items, "next_cursor": None, "has_more": False}

    async def list_goals(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text("SELECT * FROM goals WHERE user_id=:user_id ORDER BY updated_at DESC"),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
            return {"items": [dict(row) for row in rows], "next_cursor": None, "has_more": False}

    async def list_revisions(self, user_id: UUID, plan_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT id,plan_id,revision,status,objective_summary,start_date,end_date,weekly_minutes,"
                            "change_reason,approved_at,created_at FROM plan_revisions "
                            "WHERE user_id=:user_id AND plan_id=:plan_id ORDER BY revision DESC"
                        ),
                        {"user_id": user_id, "plan_id": plan_id},
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                plan = await connection.execute(
                    text("SELECT 1 FROM plans WHERE id=:id AND user_id=:user_id"),
                    {"id": plan_id, "user_id": user_id},
                )
                if plan.first() is None:
                    raise not_found()
            return {"items": [dict(row) for row in rows], "next_cursor": None, "has_more": False}

    async def get_revision(
        self, user_id: UUID, plan_id: UUID, revision_id: UUID
    ) -> dict[str, object]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM plan_revisions WHERE id=:id AND plan_id=:plan_id AND user_id=:user_id"
                    ),
                    {"id": revision_id, "plan_id": plan_id, "user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            return row

    async def get_plan(self, user_id: UUID, plan_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            plan = _row(
                await connection.execute(
                    text(
                        "SELECT p.*,g.title AS goal_title,g.goal_type,g.target_date,g.status AS goal_status "
                        "FROM plans p JOIN goals g ON g.id=p.goal_id WHERE p.id=:id AND p.user_id=:user_id"
                    ),
                    {"id": plan_id, "user_id": user_id},
                )
            )
            if plan is None:
                raise not_found()
            revision = _row(
                await connection.execute(
                    text("SELECT * FROM plan_revisions WHERE id=:id AND user_id=:user_id"),
                    {"id": plan["current_revision_id"], "user_id": user_id},
                )
            )
            return {
                "id": plan["id"],
                "goal": {
                    "id": plan["goal_id"],
                    "title": plan["goal_title"],
                    "goal_type": plan["goal_type"],
                    "target_date": plan["target_date"],
                    "status": plan["goal_status"],
                },
                "title": plan["title"],
                "status": plan["status"],
                "current_revision": revision,
                "updated_at": plan["updated_at"],
            }

    async def list_tasks(
        self, user_id: UUID, date_from: date | None, date_to: date | None
    ) -> dict[str, object]:
        date_from = date_from or datetime.now(UTC).date()
        date_to = date_to or date_from + timedelta(days=7)
        if (date_to - date_from).days > 93:
            raise ButlerError("INVALID_DATE_RANGE", "任务查询范围不能超过 93 天", 400)
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT t.*,p.title AS plan_title FROM tasks t JOIN plans p ON p.id=t.plan_id "
                            "WHERE t.user_id=:user_id AND t.scheduled_date BETWEEN :date_from AND :date_to "
                            "ORDER BY t.scheduled_date,t.priority,t.created_at"
                        ),
                        {"user_id": user_id, "date_from": date_from, "date_to": date_to},
                    )
                )
                .mappings()
                .all()
            )
            return {"items": [dict(row) for row in rows], "next_cursor": None, "has_more": False}

    async def get_task(self, user_id: UUID, task_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT t.*,p.title AS plan_title FROM tasks t JOIN plans p ON p.id=t.plan_id "
                        "WHERE t.id=:id AND t.user_id=:user_id"
                    ),
                    {"id": task_id, "user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            return row

    async def execute_task(
        self, user_id: UUID, task_id: UUID, request: TaskExecutionRequest
    ) -> dict[str, object]:
        async with self.database.transaction() as connection:
            duplicate = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM task_executions WHERE user_id=:user_id AND client_execution_id=:client_id"
                    ),
                    {"user_id": user_id, "client_id": request.client_execution_id},
                )
            )
            task = _row(
                await connection.execute(
                    text("SELECT * FROM tasks WHERE id=:id AND user_id=:user_id FOR UPDATE"),
                    {"id": task_id, "user_id": user_id},
                )
            )
            if task is None:
                raise not_found()
            if duplicate:
                return {"execution": duplicate, "task": task}
            execution_id = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO task_executions(id,user_id,task_id,client_execution_id,result,duration_minutes,"
                    "feedback,outcome_data,occurred_at) VALUES(:id,:user_id,:task_id,:client_id,:result,:duration,"
                    ":feedback,CAST(:outcome AS jsonb),:occurred_at)"
                ),
                {
                    "id": execution_id,
                    "user_id": user_id,
                    "task_id": task_id,
                    "client_id": request.client_execution_id,
                    "result": request.result,
                    "duration": request.duration_minutes,
                    "feedback": request.feedback,
                    "outcome": request.model_dump_json(include={"outcome_data"}),
                    "occurred_at": request.occurred_at,
                },
            )
            new_status = {
                "COMPLETED": "DONE",
                "SKIPPED": "SKIPPED",
                "PARTIAL": task["status"],
            }[request.result]
            await connection.execute(
                text(
                    "UPDATE tasks SET status=CAST(:status AS varchar),completed_at=CASE "
                    "WHEN CAST(:status AS varchar)='DONE' THEN :occurred ELSE completed_at END,"
                    "updated_at=now() WHERE id=:id"
                ),
                {"status": new_status, "occurred": request.occurred_at, "id": task_id},
            )
            return {
                "execution": {
                    "id": execution_id,
                    "task_id": task_id,
                    "result": request.result,
                    "duration_minutes": request.duration_minutes,
                    "occurred_at": request.occurred_at,
                },
                "task": {"id": task_id, "status": new_status},
            }

    async def create_upload_intent(
        self, user_id: UUID, request: UploadIntentRequest
    ) -> dict[str, object]:
        file_id = uuid4()
        object_key = f"users/{user_id}/{file_id}"
        ticket = issue_signed_ticket(
            file_id, self.settings.stream_ticket_secret, self.settings.stream_ticket_seconds
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO stored_files(id,user_id,purpose,original_filename,object_key,mime_type,size_bytes,sha256,"
                    "upload_status,scan_status) VALUES(:id,:user_id,:purpose,:filename,:object_key,:mime,:size,:sha,"
                    "'PENDING','PENDING')"
                ),
                {
                    "id": file_id,
                    "user_id": user_id,
                    "purpose": request.purpose,
                    "filename": Path(request.filename).name,
                    "object_key": object_key,
                    "mime": request.declared_mime_type,
                    "size": request.size_bytes,
                    "sha": request.sha256,
                },
            )
        return {
            "file": {
                "id": file_id,
                "purpose": request.purpose,
                "original_filename": Path(request.filename).name,
                "upload_status": "PENDING",
                "scan_status": "PENDING",
            },
            "upload": {
                "method": "PUT",
                "url": f"{self.settings.public_base_url}/v1/files/{file_id}/content?ticket={ticket}",
                "headers": {"Content-Type": request.declared_mime_type},
                "expires_at": datetime.now(UTC)
                + timedelta(seconds=self.settings.stream_ticket_seconds),
            },
        }

    async def store_local_upload(self, file_id: UUID, content: bytes) -> None:
        async with self.database.transaction() as connection:
            row = _row(
                await connection.execute(
                    text("SELECT * FROM stored_files WHERE id=:id FOR UPDATE"), {"id": file_id}
                )
            )
            if row is None or row["upload_status"] != "PENDING":
                raise not_found()
            if len(content) != row["size_bytes"]:
                raise ButlerError("FILE_SIZE_MISMATCH", "文件大小与上传意图不一致", 400)
            target = (self.settings.object_storage_local_path / row["object_key"]).resolve()
            root = self.settings.object_storage_local_path.resolve()
            if not target.is_relative_to(root):
                raise ButlerError("INVALID_OBJECT_KEY", "文件路径无效", 400)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            await connection.execute(
                text(
                    "UPDATE stored_files SET upload_status='UPLOADED',updated_at=now() WHERE id=:id"
                ),
                {"id": file_id},
            )

    async def read_local_file(self, file_id: UUID) -> tuple[bytes, str, str]:
        """在短期票据已验证后读取本地对象；不暴露 object_key。"""

        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT object_key,mime_type,original_filename,upload_status,scan_status "
                        "FROM stored_files WHERE id=:id"
                    ),
                    {"id": file_id},
                )
            )
        if row is None or row["upload_status"] != "VERIFIED" or row["scan_status"] != "CLEAN":
            raise not_found()
        target = self.settings.object_storage_local_path / row["object_key"]
        if not target.exists():
            raise not_found()
        return target.read_bytes(), row["mime_type"], row["original_filename"]

    async def complete_upload(
        self, user_id: UUID, file_id: UUID, request: CompleteUploadRequest
    ) -> dict[str, object]:
        async with self.database.transaction() as connection:
            row = _row(
                await connection.execute(
                    text("SELECT * FROM stored_files WHERE id=:id AND user_id=:user_id FOR UPDATE"),
                    {"id": file_id, "user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            target = self.settings.object_storage_local_path / row["object_key"]
            if (
                not target.exists()
                or hashlib.sha256(target.read_bytes()).hexdigest() != request.sha256
            ):
                raise ButlerError("FILE_HASH_MISMATCH", "文件校验失败", 400)
            if row["sha256"] != request.sha256:
                raise ButlerError("FILE_HASH_MISMATCH", "文件校验失败", 400)
            # 验证环境使用确定性扫描器；生产配置不得把该结果当作真实恶意软件扫描。
            await connection.execute(
                text(
                    "UPDATE stored_files SET upload_status='VERIFIED',scan_status='CLEAN',updated_at=now() WHERE id=:id"
                ),
                {"id": file_id},
            )
        return await self.get_file(user_id, file_id)

    async def get_file(self, user_id: UUID, file_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT sf.id,sf.purpose,sf.original_filename,sf.mime_type,sf.size_bytes,"
                        "sf.upload_status,sf.scan_status,sf.created_at,kd.ingestion_status AS knowledge_status "
                        "FROM stored_files sf LEFT JOIN knowledge_documents kd ON kd.stored_file_id=sf.id "
                        "WHERE sf.id=:id AND sf.user_id=:user_id AND sf.upload_status<>'DELETED'"
                    ),
                    {"id": file_id, "user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            row["status"] = (
                "READY"
                if row["upload_status"] == "VERIFIED" and row["scan_status"] == "CLEAN"
                else "PROCESSING"
            )
            return row

    async def file_download(self, user_id: UUID, file_id: UUID) -> dict[str, object]:
        file = await self.get_file(user_id, file_id)
        if file["status"] != "READY":
            raise not_found()
        ticket = issue_signed_ticket(
            file_id, self.settings.stream_ticket_secret, self.settings.stream_ticket_seconds
        )
        return {
            "url": f"{self.settings.public_base_url}/v1/files/{file_id}/content?ticket={ticket}",
            "expires_at": datetime.now(UTC)
            + timedelta(seconds=self.settings.stream_ticket_seconds),
            "filename": file["original_filename"],
        }

    async def delete_file(self, user_id: UUID, file_id: UUID) -> dict[str, object]:
        async with self.database.transaction() as connection:
            result = await connection.execute(
                text(
                    "UPDATE stored_files SET upload_status='DELETED',deleted_at=now(),updated_at=now() "
                    "WHERE id=:id AND user_id=:user_id AND upload_status<>'DELETED' "
                    "RETURNING object_key"
                ),
                {"id": file_id, "user_id": user_id},
            )
            row = result.first()
            if row is None:
                raise not_found()
            await connection.execute(
                text(
                    "UPDATE knowledge_documents SET ingestion_status='DELETING',updated_at=now() "
                    "WHERE stored_file_id=:file_id"
                ),
                {"file_id": file_id},
            )
        target = self.settings.object_storage_local_path / row[0]
        if target.exists():
            target.unlink()
        return {"id": file_id, "status": "DELETED"}

    async def get_document_access(self, user_id: UUID, document_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM knowledge_documents WHERE id=:id AND ingestion_status='READY' "
                        "AND (visibility='PUBLIC' OR owner_user_id=:user_id)"
                    ),
                    {"id": document_id, "user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            if row["source_url"]:
                return {"access_type": "EXTERNAL_URL", "url": row["source_url"], "expires_at": None}
            raise ButlerError("SOURCE_FIXTURE_ONLY", "合成验收资料没有外部原文", 409)

    async def list_files(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT sf.id,sf.purpose,sf.original_filename,sf.mime_type,sf.size_bytes,"
                            "sf.created_at,kd.ingestion_status AS knowledge_status FROM stored_files sf "
                            "LEFT JOIN knowledge_documents kd ON kd.stored_file_id=sf.id "
                            "WHERE sf.user_id=:user_id AND sf.upload_status='VERIFIED' "
                            "AND sf.scan_status='CLEAN' ORDER BY sf.created_at DESC"
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
            return {"items": [dict(row) for row in rows], "next_cursor": None, "has_more": False}

    async def get_citation(self, user_id: UUID, citation_id: UUID) -> dict[str, object]:
        """返回当前用户 run 中的来源快照，并按来源类型生成安全访问方式。"""

        async with self.database.connect() as connection:
            citation = _row(
                await connection.execute(
                    text(
                        "SELECT ci.*,kd.stored_file_id,sf.original_filename,sf.upload_status,"
                        "sf.scan_status FROM citations ci JOIN claims c ON c.id=ci.claim_id "
                        "JOIN agent_runs r ON r.id=c.agent_run_id "
                        "LEFT JOIN knowledge_chunks kc ON kc.id=ci.knowledge_chunk_id "
                        "LEFT JOIN knowledge_documents kd ON kd.id=kc.document_id "
                        "LEFT JOIN stored_files sf ON sf.id=kd.stored_file_id "
                        "WHERE ci.id=:id AND r.user_id=:user_id AND "
                        "(kd.id IS NULL OR kd.visibility='PUBLIC' OR kd.owner_user_id=:user_id)"
                    ),
                    {"id": citation_id, "user_id": user_id},
                )
            )
            if citation is None:
                raise not_found()
        access: dict[str, object] = {"type": "UNAVAILABLE", "url": None, "expires_at": None}
        source_url = citation["source_url_snapshot"]
        if source_url:
            canonical, _ = self.evidence_gate.canonicalize_url(str(source_url))
            access = {"type": "EXTERNAL_URL", "url": canonical, "expires_at": None}
        elif citation["stored_file_id"]:
            if citation["upload_status"] != "VERIFIED" or citation["scan_status"] != "CLEAN":
                raise not_found()
            expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.stream_ticket_seconds)
            ticket = issue_signed_ticket(
                UUID(str(citation["stored_file_id"])),
                self.settings.stream_ticket_secret,
                self.settings.stream_ticket_seconds,
            )
            access = {
                "type": "SIGNED_FILE",
                "url": f"{self.settings.public_base_url}/v1/files/{citation['stored_file_id']}/content?ticket={ticket}",
                "expires_at": expires_at,
            }
        return {
            "id": citation["id"],
            "source_type": citation["source_type"],
            "title": citation["source_title_snapshot"],
            "source_organization": citation["source_organization_snapshot"],
            "domain": citation["source_domain_snapshot"],
            "published_at": citation["published_at_snapshot"],
            "retrieved_at": citation["retrieved_at_snapshot"],
            "evidence_excerpt": citation["evidence_excerpt"],
            "access": access,
        }

    async def worker_poll_once(self, worker_id: UUID) -> bool:
        """领取并执行一个 run；网络/模型实现接入时应在领取事务提交后调用。"""

        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM agent_runs WHERE status='QUEUED' OR "
                        "(status IN ('RUNNING','CANCEL_REQUESTED') AND lease_expires_at<now()) "
                        "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
                    )
                )
            )
            if run is None:
                return False
            if run["status"] == "CANCEL_REQUESTED":
                await connection.execute(
                    text("UPDATE agent_runs SET status='CANCELLED',updated_at=now() WHERE id=:id"),
                    {"id": run["id"]},
                )
                await self._append_event(
                    connection, run["id"], run["user_id"], "run.cancelled", {}, run["attempt"]
                )
                return True
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='RUNNING',worker_id=:worker,heartbeat_at=now(),"
                    "lease_expires_at=now()+(:lease || ' seconds')::interval,updated_at=now() WHERE id=:id"
                ),
                {"worker": worker_id, "lease": self.settings.worker_lease_seconds, "id": run["id"]},
            )
            await self._append_event(
                connection,
                run["id"],
                run["user_id"],
                "run.status",
                {"status": "RUNNING"},
                run["attempt"],
            )
        try:
            await self._execute_run(UUID(str(run["id"])))
        except ButlerError as exc:
            await self._fail_run(UUID(str(run["id"])), exc)
        except Exception:
            await self._fail_run(
                UUID(str(run["id"])),
                ButlerError("AGENT_INTERNAL_ERROR", "管家暂时无法完成处理", 500, True),
            )
        return True

    async def scheduler_poll_once(self) -> bool:
        """处理一个可安全重试的通知或账号治理作业。"""

        if await self._delete_one_knowledge_vector():
            return True
        if await self._ingest_one_private_file():
            return True
        async with self.database.transaction() as connection:
            notification = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM notification_jobs WHERE status IN ('PENDING','RETRY') "
                        "AND COALESCE(next_attempt_at,scheduled_at)<=now() ORDER BY scheduled_at "
                        "FOR UPDATE SKIP LOCKED LIMIT 1"
                    )
                )
            )
            if notification:
                await connection.execute(
                    text(
                        "UPDATE notification_jobs SET status='SENT',attempt_count=attempt_count+1,"
                        "provider_message_id=idempotency_key,sent_at=now(),updated_at=now() WHERE id=:id"
                    ),
                    {"id": notification["id"]},
                )
                return True
            deleting = _row(
                await connection.execute(
                    text(
                        "SELECT u.id FROM users u WHERE u.status='DELETING' AND NOT EXISTS "
                        "(SELECT 1 FROM knowledge_documents kd WHERE kd.owner_user_id=u.id) "
                        "FOR UPDATE OF u SKIP LOCKED LIMIT 1"
                    )
                )
            )
            if deleting:
                # 用户行作为删除墓碑保留；级联删除其他业务事实后不可再次登录。
                await connection.execute(
                    text("DELETE FROM user_identities WHERE user_id=:id"), {"id": deleting["id"]}
                )
                await connection.execute(
                    text(
                        "UPDATE users SET nickname=NULL,status='DELETED',deleted_at=now(),updated_at=now() WHERE id=:id"
                    ),
                    {"id": deleting["id"]},
                )
                return True
        return False

    async def _delete_one_knowledge_vector(self) -> bool:
        """先隐藏删除中的资料，再清理 Qdrant，最后删除 PostgreSQL 文档投影。

        Qdrant 失败时文档保留 ``DELETING`` 供 Scheduler 重试；检索查询只接受
        ``READY``，因此失败不会让已删除资料重新可见。
        """

        async with self.database.transaction() as connection:
            document = _row(
                await connection.execute(
                    text(
                        "SELECT kd.id,kd.owner_user_id FROM knowledge_documents kd "
                        "LEFT JOIN users u ON u.id=kd.owner_user_id WHERE kd.owner_user_id IS NOT NULL "
                        "AND (kd.ingestion_status='DELETING' OR u.status='DELETING') "
                        "ORDER BY kd.updated_at FOR UPDATE OF kd SKIP LOCKED LIMIT 1"
                    )
                )
            )
            if document is None:
                return False
            await connection.execute(
                text(
                    "UPDATE knowledge_documents SET ingestion_status='DELETING',updated_at=now() "
                    "WHERE id=:id"
                ),
                {"id": document["id"]},
            )
        try:
            await self.vector_store.delete_document(
                UUID(str(document["owner_user_id"])), UUID(str(document["id"]))
            )
        except VectorStoreError:
            return True
        async with self.database.transaction() as connection:
            await connection.execute(
                text("DELETE FROM knowledge_documents WHERE id=:id"), {"id": document["id"]}
            )
        return True

    async def _ingest_one_private_file(self) -> bool:
        """领取并入库一个 CLEAN 私有资料，网络与解析工作均在事务外执行。

        ``knowledge_documents.stored_file_id`` 唯一约束使重复调度只复用同一文档；
        chunk/point ID 根据内容稳定生成，Worker 崩溃后的重试不会产生重复向量。
        """

        async with self.database.transaction() as connection:
            file = _row(
                await connection.execute(
                    text(
                        "SELECT sf.* FROM stored_files sf LEFT JOIN knowledge_documents kd "
                        "ON kd.stored_file_id=sf.id WHERE sf.upload_status='VERIFIED' "
                        "AND sf.scan_status='CLEAN' AND sf.purpose IN ('STUDY_MATERIAL','CHAT_ATTACHMENT') "
                        "AND sf.mime_type=ANY(:mimes) AND (kd.id IS NULL OR (kd.ingestion_status='FAILED' "
                        "AND kd.updated_at<now()-interval '10 minutes') "
                        "OR (kd.ingestion_status='PROCESSING' AND kd.updated_at<now()-interval '10 minutes')) "
                        "ORDER BY (kd.id IS NULL) DESC,sf.created_at "
                        "FOR UPDATE OF sf SKIP LOCKED LIMIT 1"
                    ),
                    {"mimes": list(SUPPORTED_RAG_MIME_TYPES)},
                )
            )
            if file is None:
                return False
            document_id = uuid5(UUID(str(file["id"])), "knowledge-document")
            await connection.execute(
                text(
                    "INSERT INTO knowledge_documents(id,owner_user_id,visibility,domain,title,"
                    "source_organization,source_level,object_key,mime_type,sha256,retrieved_at,"
                    "ingestion_status,stored_file_id) VALUES(:id,:user_id,'PRIVATE','USER_MATERIAL',"
                    ":title,'我的资料','PRIVATE',:object_key,:mime,:sha,now(),'PROCESSING',:file_id) "
                    "ON CONFLICT (stored_file_id) WHERE stored_file_id IS NOT NULL DO UPDATE SET "
                    "ingestion_status='PROCESSING',error_code=NULL,updated_at=now()"
                ),
                {
                    "id": document_id,
                    "user_id": file["user_id"],
                    "title": file["original_filename"],
                    "object_key": file["object_key"],
                    "mime": file["mime_type"],
                    "sha": file["sha256"],
                    "file_id": file["id"],
                },
            )

        try:
            path = (self.settings.object_storage_local_path / file["object_key"]).resolve()
            root = self.settings.object_storage_local_path.resolve()
            if not path.is_relative_to(root):
                raise ValueError("invalid object key")
            raw = await asyncio.to_thread(path.read_bytes)
            extracted = await asyncio.to_thread(extract_text, raw, str(file["mime_type"]))
            chunks = chunk_text(extracted)
            if not chunks:
                raise ValueError("document contains no extractable text")
            points: list[VectorPoint] = []
            rows: list[dict[str, object]] = []
            for chunk in chunks:
                digest = hashlib.sha256(chunk.content.encode()).hexdigest()
                chunk_id = uuid5(document_id, f"chunk:{chunk.index}:{digest}")
                vector = await self.embedding_provider.embed(chunk.content)
                if len(vector) != self.settings.embedding_dimensions:
                    raise ValueError("embedding dimension mismatch")
                points.append(
                    VectorPoint(
                        point_id=chunk_id,
                        vector=tuple(vector),
                        tenant_id=UUID(str(file["user_id"])),
                        document_id=document_id,
                        chunk_id=chunk_id,
                    )
                )
                rows.append(
                    {
                        "id": chunk_id,
                        "index": chunk.index,
                        "content": chunk.content,
                        "tokens": max(1, len(chunk.content) // 2),
                        "hash": digest,
                    }
                )
            await self.vector_store.upsert(tuple(points))
            async with self.database.transaction() as connection:
                await connection.execute(
                    text("DELETE FROM knowledge_chunks WHERE document_id=:document"),
                    {"document": document_id},
                )
                for row in rows:
                    await connection.execute(
                        text(
                            "INSERT INTO knowledge_chunks(id,document_id,chunk_index,content,token_count,"
                            "content_hash,embedding_model,qdrant_collection,qdrant_point_id,vector_status) "
                            "VALUES(:id,:document,:index,:content,:tokens,:hash,:model,:collection,:id,'READY')"
                        ),
                        {
                            **row,
                            "document": document_id,
                            "model": self.settings.embedding_model,
                            "collection": self.settings.qdrant_collection,
                        },
                    )
                await connection.execute(
                    text(
                        "UPDATE knowledge_documents SET ingestion_status='READY',error_code=NULL,"
                        "updated_at=now() WHERE id=:id"
                    ),
                    {"id": document_id},
                )
        except (OSError, UnicodeError, ValueError, VectorStoreError):
            async with self.database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE knowledge_documents SET ingestion_status='FAILED',"
                        "error_code='KNOWLEDGE_INGESTION_FAILED',updated_at=now() WHERE id=:id"
                    ),
                    {"id": document_id},
                )
        return True

    async def _execute_run(self, run_id: UUID) -> None:
        """在事务外完成联网检索，再用短事务原子写入回答与引用。

        run 领取和最终持久化分别加锁；搜索期间用户可能请求取消，因此写入前
        必须重新检查 ``RUNNING``。外部查询使用脱敏后的最小文本，不携带身份。
        """

        async with self.database.connect() as connection:
            snapshot = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id"), {"id": run_id}
                )
            )
            if snapshot is None or snapshot["status"] != "RUNNING":
                return
            request_message = _row(
                await connection.execute(
                    text("SELECT content,structured_content FROM messages WHERE id=:id"),
                    {"id": snapshot["request_message_id"]},
                )
            ) or {"content": "", "structured_content": {}}
            current_input = str(request_message.get("content") or "")
            request_context = request_message.get("structured_content")
            request_context = request_context if isinstance(request_context, dict) else {}
            content = str(
                (
                    await connection.execute(
                        text(
                            "SELECT string_agg(content,' ' ORDER BY created_at,id) FROM messages "
                            "WHERE agent_run_id=:id AND role='USER'"
                        ),
                        {"id": run_id},
                    )
                ).scalar_one_or_none()
                or ""
            )
            attachment_file_ids = tuple(
                UUID(str(row[0]))
                for row in (
                    await connection.execute(
                        text(
                            "SELECT DISTINCT ma.file_id FROM message_attachments ma "
                            "JOIN messages m ON m.id=ma.message_id "
                            "WHERE m.agent_run_id=:id AND m.role='USER'"
                        ),
                        {"id": run_id},
                    )
                ).all()
            )

        raw_results: tuple[SearchResult, ...] = ()
        is_plan = bool(PLAN_PATTERN.search(content) and PLAN_ACTION_PATTERN.search(content))
        availability_candidate: AvailabilityInterpretationV1 | None = None
        confirmed_availability: AvailabilityInterpretationV1 | None = None
        revise_availability = False
        if is_plan and snapshot["pending_action"] != "APPROVAL_RESUME":
            phase = str(request_context.get("card_phase") or "")
            selected_options = request_context.get("selected_options")
            selected_option = (
                selected_options[0]
                if isinstance(selected_options, list)
                and selected_options
                and isinstance(selected_options[0], dict)
                else {}
            )
            selected_option_id = str(selected_option.get("id") or "")
            if phase == "CONFIRM_AVAILABILITY" and selected_option_id == "confirm-availability":
                try:
                    validated_confirmation = AvailabilityInterpreter.normalize(
                        AvailabilityInterpretationV1.model_validate(
                            request_context.get("interpretation")
                        )
                    )
                    if validated_confirmation.status == "COMPLETE":
                        confirmed_availability = validated_confirmation
                    else:
                        availability_candidate = validated_confirmation
                except ValueError:
                    availability_candidate = AvailabilityInterpretationV1(
                        status="NEEDS_CLARIFICATION",
                        question="时间确认信息已经失效，请重新描述你的学习时间。",
                    )
            elif phase == "CONFIRM_AVAILABILITY" and selected_option_id == "revise-availability":
                revise_availability = True
            elif isinstance(selected_option.get("availability"), dict):
                try:
                    availability_candidate = AvailabilityInterpreter.normalize(
                        AvailabilityInterpretationV1.model_validate(selected_option["availability"])
                    )
                except ValueError:
                    availability_candidate = AvailabilityInterpretationV1(
                        status="NEEDS_CLARIFICATION",
                        question="这个快捷选项暂时不可用，请直接描述你的学习时间。",
                    )
            elif current_input and (
                snapshot["pending_action"] == "INPUT_RESUME"
                or bool(TIME_PATTERN.search(current_input))
            ):
                availability_candidate = await self.availability_interpreter.interpret(
                    current_input
                )

        # 计划类请求先确认时间，再进行可能产生费用的检索；确认前不预取或缓存用户查询。
        can_retrieve = snapshot["pending_action"] != "APPROVAL_RESUME" and (
            not is_plan or confirmed_availability is not None
        )
        plan_ready = is_plan and confirmed_availability is not None
        needs_private = can_retrieve and (
            bool(attachment_file_ids) or bool(PRIVATE_SEARCH_PATTERN.search(content))
        )
        needs_web = can_retrieve and (
            plan_ready
            or bool(WEB_FORCE_PATTERN.search(content))
            or (bool(SEARCH_PATTERN.search(content)) and not attachment_file_ids)
        )
        query = minimize_public_query(content) or "公务员备考资料"
        if needs_private:
            await self._emit_progress(snapshot, "RETRIEVING_PRIVATE")
            try:
                raw_results += await self._retrieve_private_evidence(
                    UUID(str(snapshot["user_id"])), query, attachment_file_ids
                )
            except VectorStoreError as exc:
                raise ButlerError(
                    "PRIVATE_RETRIEVAL_UNAVAILABLE", "我的资料暂时无法检索，请稍后重试", 503, True
                ) from exc
        if needs_web:
            await self._emit_progress(snapshot, "SEARCHING_WEB")
            try:
                results = await self.search_provider.search(
                    SearchRequest(query=query, max_results=self.settings.search_max_results)
                )
            except SearchUnavailableError as exc:
                raise ButlerError(
                    "SEARCH_PROVIDER_UNAVAILABLE", "联网搜索暂时不可用，请稍后重试", 503, True
                ) from exc
            except SearchError as exc:
                raise ButlerError("SEARCH_PROVIDER_INVALID", "联网搜索返回无效结果", 502) from exc
            raw_results += results
        evidence = self.evidence_gate.normalize(raw_results, limit=self.settings.search_max_results)
        needs_search = needs_private or needs_web
        if needs_search:
            await self._emit_progress(snapshot, "ORGANIZING_CITATIONS")

        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                )
            )
            if run is None or run["status"] != "RUNNING":
                return
            # 普通管家会话只有在识别到明确的考公意图后才绑定专业 Agent。
            # 专业会话在创建 run 时已经固定选择，worker 不得覆盖或改路由。
            if run["selected_user_agent_id"] is None and PLAN_PATTERN.search(content):
                selected_agent_id = (
                    await connection.execute(
                        text(
                            "SELECT ua.id FROM user_agents ua "
                            "JOIN agent_definitions ad ON ad.id=ua.agent_definition_id "
                            "WHERE ua.user_id=:user_id AND ua.status='ACTIVE' "
                            "AND ad.code='CIVIL_SERVICE_EXAM' "
                            "AND ad.status='ACTIVE' AND ad.catalog_status='AVAILABLE' LIMIT 1"
                        ),
                        {"user_id": run["user_id"]},
                    )
                ).scalar_one_or_none()
                if selected_agent_id is not None:
                    await connection.execute(
                        text(
                            "UPDATE agent_runs SET selected_user_agent_id=:selected_agent_id "
                            "WHERE id=:run_id"
                        ),
                        {"selected_agent_id": selected_agent_id, "run_id": run_id},
                    )
                    run["selected_user_agent_id"] = selected_agent_id
            if run["pending_action"] == "APPROVAL_RESUME":
                approval = _row(
                    await connection.execute(
                        text(
                            "SELECT * FROM approval_decisions WHERE agent_run_id=:id ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"id": run_id},
                    )
                )
                action = approval["action"] if approval else "REJECT"
                response = {
                    "APPROVE": "计划已确认，正式任务已经生成。你可以在首页开始今天的学习。",
                    "REJECT": "好的，这份草案不会生效，也没有创建任何正式任务。",
                    "EDIT": "收到修改意见。我会在下一版草案中降低负荷并保留必要复习。",
                }.get(action, "审批已处理。")
                if action == "EDIT" and approval:
                    await self._regenerate_approval(connection, run, approval)
                    return
                await self._complete_run(connection, run, response)
                return
            if is_plan:
                if confirmed_availability is not None:
                    await self._create_plan_draft(
                        connection, run, content, evidence, confirmed_availability
                    )
                elif revise_availability:
                    await self._interrupt_for_availability_clarification(
                        connection, run, "好的，请重新描述你的学习时间。"
                    )
                elif availability_candidate is not None:
                    await self._interrupt_for_availability_confirmation(
                        connection, run, availability_candidate
                    )
                else:
                    await self._interrupt_for_input(connection, run)
                return
            if needs_search:
                await self._complete_rag_run(connection, run, evidence)
                return
            await self._complete_run(
                connection,
                run,
                "我目前可以协助公务员备考规划、资料检索、任务跟进和计划调整。请告诉我目标考试与可投入时间。",
            )

    async def _retrieve_private_evidence(
        self,
        user_id: UUID,
        query: str,
        allowed_file_ids: tuple[UUID, ...],
    ) -> tuple[SearchResult, ...]:
        """向量召回后重新读取 PostgreSQL 所有权事实，拒绝信任 Qdrant payload 授权。"""

        vector = await self.embedding_provider.embed(query)
        chunk_ids = await self.vector_store.search(
            user_id, vector, self.settings.search_max_results
        )
        if not chunk_ids:
            return ()
        parameters: dict[str, object] = {"user_id": user_id, "chunk_ids": list(chunk_ids)}
        if allowed_file_ids:
            parameters["file_ids"] = list(allowed_file_ids)
            query_text = text(
                "SELECT kc.id,kc.content,kd.title,kd.stored_file_id FROM knowledge_chunks kc "
                "JOIN knowledge_documents kd ON kd.id=kc.document_id "
                "JOIN stored_files sf ON sf.id=kd.stored_file_id "
                "WHERE kc.id=ANY(:chunk_ids) AND kd.owner_user_id=:user_id "
                "AND kd.visibility='PRIVATE' AND kd.ingestion_status='READY' "
                "AND sf.upload_status='VERIFIED' AND sf.scan_status='CLEAN' "
                "AND kd.stored_file_id=ANY(:file_ids)"
            )
        else:
            query_text = text(
                "SELECT kc.id,kc.content,kd.title,kd.stored_file_id FROM knowledge_chunks kc "
                "JOIN knowledge_documents kd ON kd.id=kc.document_id "
                "JOIN stored_files sf ON sf.id=kd.stored_file_id "
                "WHERE kc.id=ANY(:chunk_ids) AND kd.owner_user_id=:user_id "
                "AND kd.visibility='PRIVATE' AND kd.ingestion_status='READY' "
                "AND sf.upload_status='VERIFIED' AND sf.scan_status='CLEAN'"
            )
        async with self.database.connect() as connection:
            rows = (await connection.execute(query_text, parameters)).mappings().all()
        by_id = {UUID(str(row["id"])): row for row in rows}
        return tuple(
            SearchResult(
                evidence_ref=f"private-{chunk_id}",
                title=str(by_id[chunk_id]["title"]),
                source_organization="我的资料",
                content=str(by_id[chunk_id]["content"]),
                score=max(0.0, 1.0 - index * 0.01),
                url=None,
                source_type="PRIVATE_FILE",
                knowledge_chunk_id=chunk_id,
            )
            for index, chunk_id in enumerate(chunk_ids)
            if chunk_id in by_id
        )

    async def _emit_progress(self, run: dict[str, Any], code: str) -> None:
        """在独立短事务中持久化网络调用前后的预定义进度，不泄露查询正文。"""

        async with self.database.transaction() as connection:
            current = _row(
                await connection.execute(
                    text("SELECT status FROM agent_runs WHERE id=:id FOR UPDATE"),
                    {"id": run["id"]},
                )
            )
            if current is None or current["status"] != "RUNNING":
                return
            await self._append_event(
                connection, run["id"], run["user_id"], "progress", {"code": code}, run["attempt"]
            )

    async def _interrupt_for_input(self, connection: AsyncConnection, run: dict[str, Any]) -> None:
        content = "请直接描述你的学习时间，例如：每天 1 小时，周末不学习。"
        card: dict[str, object] = {
            "schema_version": "1.0",
            "cards": [
                {
                    "schema_version": "1.0",
                    "card_id": str(uuid4()),
                    "card_type": "SelectionCard",
                    "entity_refs": {},
                    "payload": {
                        "question": content,
                        "description": "可以在下方输入自然语言，也可以选择一个常用安排。",
                        "input_mode": "NATURAL_LANGUAGE",
                        "phase": "COLLECT_AVAILABILITY",
                        "input_placeholder": "例如：每天 1 小时，周末不学习",
                        "options": list(quick_availability_options()),
                    },
                    "actions": [
                        {
                            "action_id": "submit-selection",
                            "action_type": "SUBMIT_SELECTION",
                            "label": "确认选择",
                        }
                    ],
                }
            ],
        }
        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,structured_content=CAST(:cards AS jsonb),"
                "updated_at=now() WHERE id=:id"
            ),
            {"content": content, "cards": _json(card), "id": run["response_message_id"]},
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='AWAITING_INPUT',updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "interrupt",
            {"type": "INPUT", "message": content, "cards": card["cards"]},
            run["attempt"],
        )

    async def _interrupt_for_availability_confirmation(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        interpretation: AvailabilityInterpretationV1,
    ) -> None:
        """持久化服务端解析候选，等待用户显式确认后才允许 Planner 使用。"""

        if interpretation.status != "COMPLETE" or interpretation.weekly_minutes is None:
            await self._interrupt_for_availability_clarification(
                connection,
                run,
                interpretation.question or "请重新描述你的学习时间。",
            )
            return
        content = f"我理解的是：{interpretation.summary}。请确认是否正确。"
        card: dict[str, object] = {
            "schema_version": "1.0",
            "cards": [
                {
                    "schema_version": "1.0",
                    "card_id": str(uuid4()),
                    "card_type": "SelectionCard",
                    "entity_refs": {},
                    "payload": {
                        "question": "确认学习时间",
                        "description": interpretation.summary,
                        "input_mode": "NATURAL_LANGUAGE",
                        "phase": "CONFIRM_AVAILABILITY",
                        "input_placeholder": "如需修改，也可以直接输入新的时间安排",
                        "interpretation": interpretation.model_dump(mode="json"),
                        "options": [
                            {"id": "confirm-availability", "label": "确认并生成计划"},
                            {"id": "revise-availability", "label": "重新描述"},
                        ],
                    },
                    "actions": [
                        {
                            "action_id": "submit-selection",
                            "action_type": "SUBMIT_SELECTION",
                            "label": "提交",
                        }
                    ],
                }
            ],
        }
        await self._write_input_interrupt(connection, run, content, card)

    async def _interrupt_for_availability_clarification(
        self, connection: AsyncConnection, run: dict[str, Any], question: str
    ) -> None:
        """在解析含糊、冲突或用户要求重写时恢复自然语言输入状态。"""

        content = question
        card: dict[str, object] = {
            "schema_version": "1.0",
            "cards": [
                {
                    "schema_version": "1.0",
                    "card_id": str(uuid4()),
                    "card_type": "SelectionCard",
                    "entity_refs": {},
                    "payload": {
                        "question": question,
                        "description": "请在下方输入具体的每天或每周学习时间。",
                        "input_mode": "NATURAL_LANGUAGE",
                        "phase": "COLLECT_AVAILABILITY",
                        "input_placeholder": "例如：工作日每天 1 小时，周末休息",
                        "options": list(quick_availability_options()),
                    },
                    "actions": [
                        {
                            "action_id": "submit-selection",
                            "action_type": "SUBMIT_SELECTION",
                            "label": "确认选择",
                        }
                    ],
                }
            ],
        }
        await self._write_input_interrupt(connection, run, content, card)

    async def _write_input_interrupt(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        content: str,
        card: dict[str, object],
    ) -> None:
        """原子完成当前回复并把同一 run 放回等待输入状态。"""

        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,"
                "structured_content=CAST(:cards AS jsonb),updated_at=now() WHERE id=:id"
            ),
            {"content": content, "cards": _json(card), "id": run["response_message_id"]},
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='AWAITING_INPUT',updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "interrupt",
            {"type": "INPUT", "message": content, "cards": card["cards"]},
            run["attempt"],
        )

    async def _create_plan_draft(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        content: str,
        evidence: tuple[NumberedEvidence, ...],
        availability: AvailabilityInterpretationV1,
    ) -> None:
        DEFAULT_CAPABILITY_REGISTRY.require("plan_draft_write", "Planner", approved=False)
        if any(item.result.source_type == "KNOWLEDGE" for item in evidence):
            await self._ensure_synthetic_source(connection)
        existing_plan = _row(
            await connection.execute(
                text(
                    "SELECT * FROM plans WHERE user_id=:user_id AND status='ACTIVE' ORDER BY created_at LIMIT 1"
                ),
                {"user_id": run["user_id"]},
            )
        )
        if existing_plan:
            plan_id = existing_plan["id"]
            goal_id = existing_plan["goal_id"]
            revision_number = int(
                (
                    await connection.execute(
                        text(
                            "SELECT COALESCE(MAX(revision),0)+1 FROM plan_revisions WHERE plan_id=:id"
                        ),
                        {"id": plan_id},
                    )
                ).scalar_one()
            )
            expected_revision = existing_plan["current_revision_id"]
            mode = "SINGLE_PLAN_ADJUST"
        else:
            goal_id, plan_id, revision_number, expected_revision = uuid4(), uuid4(), 1, None
            mode = "CREATE"
            await connection.execute(
                text(
                    "INSERT INTO goals(id,user_id,goal_type,title,status) "
                    "VALUES(:id,:user_id,'CIVIL_SERVICE_EXAM','公务员考试备考','DRAFT')"
                ),
                {"id": goal_id, "user_id": run["user_id"]},
            )
            await connection.execute(
                text(
                    "INSERT INTO plans(id,user_id,goal_id,title,status) "
                    "VALUES(:id,:user_id,:goal_id,'公务员备考','DRAFT')"
                ),
                {"id": plan_id, "user_id": run["user_id"], "goal_id": goal_id},
            )
        revision_id = uuid4()
        start = datetime.now(UTC).date()
        end = start + timedelta(days=27)
        weekly_minutes = int(availability.weekly_minutes or 0)
        tasks = self._draft_tasks_for_availability(start, availability)
        summary = f"四周基础验证计划：{availability.summary}；安排行测、申论与每周复盘"
        await connection.execute(
            text(
                "INSERT INTO plan_revisions(id,plan_id,user_id,agent_run_id,revision,status,objective_summary,"
                "start_date,end_date,weekly_minutes,change_reason,content) "
                "VALUES(:id,:plan_id,:user_id,:run_id,:revision,'DRAFT',:summary,:start,:end,"
                ":weekly_minutes,:reason,CAST(:content AS jsonb))"
            ),
            {
                "id": revision_id,
                "plan_id": plan_id,
                "user_id": run["user_id"],
                "run_id": run["id"],
                "revision": revision_number,
                "summary": summary,
                "start": start,
                "end": end,
                "weekly_minutes": weekly_minutes,
                "reason": self._safe_summary(content),
                "content": _json(
                    {
                        "tasks": tasks,
                        "availability": availability.model_dump(mode="json"),
                    }
                ),
            },
        )
        approval_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO approval_decisions(id,user_id,agent_run_id) VALUES(:id,:user_id,:run_id)"
            ),
            {"id": approval_id, "user_id": run["user_id"], "run_id": run["id"]},
        )
        await connection.execute(
            text(
                "INSERT INTO approval_decision_items(id,approval_id,plan_id,plan_revision_id,expected_current_revision_id) "
                "VALUES(:id,:approval,:plan,:revision,:expected)"
            ),
            {
                "id": uuid4(),
                "approval": approval_id,
                "plan": plan_id,
                "revision": revision_id,
                "expected": expected_revision,
            },
        )
        source_card = await self._persist_evidence(
            connection,
            run,
            evidence,
            claim_text="本计划参考了检索结果中的公务员备考科目与训练建议",
            plan_revision_id=revision_id,
        )
        card = {
            "schema_version": "1.0",
            "card_id": str(uuid4()),
            "card_type": "PlanCard",
            "entity_refs": {
                "approval_id": str(approval_id),
                "approval_version": 1,
                "items": [
                    {
                        "plan_id": str(plan_id),
                        "plan_revision_id": str(revision_id),
                        "expected_current_revision_id": str(expected_revision)
                        if expected_revision
                        else None,
                    }
                ],
            },
            "payload": {
                "mode": mode,
                "title": "公务员备考 · 四周验证计划",
                "objective_summary": summary,
                "weekly_minutes": weekly_minutes,
                "warnings": (["当前为合成离线来源，不代表真实考试公告。"] if evidence else []),
            },
            "actions": [
                {"action_id": "approve", "action_type": "APPROVE", "label": "确认计划"},
                {"action_id": "edit", "action_type": "EDIT", "label": "继续修改"},
                {"action_id": "reject", "action_type": "REJECT", "label": "拒绝"},
            ],
        }
        citation_marks = "".join(f"[{item.index}]" for item in evidence)
        response = (
            f"我已参考检索来源生成计划草案。{citation_marks}请使用卡片按钮确认、修改或拒绝。"
            if evidence
            else "我已生成计划草案。请使用卡片按钮确认、修改或拒绝。"
        )
        cards = [card, *([source_card] if source_card else [])]
        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,structured_content=CAST(:cards AS jsonb),"
                "updated_at=now() WHERE id=:id"
            ),
            {
                "content": response,
                "cards": _json({"cards": cards}),
                "id": run["response_message_id"],
            },
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='AWAITING_APPROVAL',updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "message.completed",
            {"message_id": str(run["response_message_id"]), "content": response, "cards": cards},
            run["attempt"],
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "interrupt",
            {"type": "APPROVAL", "approval_id": str(approval_id), "approval_version": 1},
            run["attempt"],
        )

    async def _complete_rag_run(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        evidence: tuple[NumberedEvidence, ...],
    ) -> None:
        """生成确定性 RAG 回答，并在同一事务保存事实、引用和 SourceCard。"""

        if not evidence:
            await self._complete_run(
                connection,
                run,
                "暂时没有找到可引用的来源，因此我不会基于未核实信息给出具体结论。你可以稍后重试或补充资料。",
            )
            return
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "progress",
            {"code": "GENERATING_ANSWER"},
            run["attempt"],
        )
        answer = RagAnswerV1(
            segments=(
                AnswerSegmentV1(
                    text=evidence[0].result.content[:800],
                    evidence_refs=(evidence[0].result.evidence_ref,),
                ),
            ),
            warnings=("合成离线来源仅用于验收，不代表真实考试公告。",)
            if evidence[0].result.source_type == "KNOWLEDGE"
            else (),
        )
        response = self.evidence_gate.render(answer, evidence)
        source_card = await self._persist_evidence(
            connection, run, evidence, claim_text=answer.segments[0].text
        )
        await self._complete_run(
            connection,
            run,
            response,
            cards=[source_card] if source_card else [],
        )

    async def _persist_evidence(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        evidence: tuple[NumberedEvidence, ...],
        *,
        claim_text: str,
        plan_revision_id: UUID | None = None,
    ) -> dict[str, object] | None:
        """原子保存一条回答 Claim 与有序 Citation，并返回只读 SourceCard。

        SourceCard 使用数据库生成的 citation ID，客户端不能根据标题或 URL
        反推实体。外部 URL 已在 EvidenceGate 中规范化；私有来源仍需在详情
        查询时重新校验用户所有权。
        """

        if not evidence:
            return None
        if any(item.result.source_type == "KNOWLEDGE" for item in evidence):
            await self._ensure_synthetic_source(connection)
        claim_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO claims(id,agent_run_id,plan_revision_id,claim_key,claim_text,claim_type) "
                "VALUES(:id,:run,:revision,:key,:claim,'FACT')"
            ),
            {
                "id": claim_id,
                "run": run["id"],
                "revision": plan_revision_id,
                "key": f"rag-answer-{claim_id}",
                "claim": claim_text[:4000],
            },
        )
        citation_ids: list[str] = []
        sources: list[dict[str, object]] = []
        for item in evidence:
            citation_id = uuid4()
            source_type = item.result.source_type
            knowledge_chunk_id = item.result.knowledge_chunk_id
            if source_type == "KNOWLEDGE" and knowledge_chunk_id is None:
                knowledge_chunk_id = PUBLIC_CHUNK_ID
            await connection.execute(
                text(
                    "INSERT INTO citations(id,claim_id,knowledge_chunk_id,source_url_snapshot,"
                    "evidence_excerpt,relation,relevance_score,source_type,source_title_snapshot,"
                    "source_organization_snapshot,source_domain_snapshot,published_at_snapshot,"
                    "retrieved_at_snapshot,source_rank) VALUES(:id,:claim,:chunk,:url,:excerpt,'SUPPORTS',"
                    ":score,:source_type,:title,:organization,:domain,:published,now(),:rank)"
                ),
                {
                    "id": citation_id,
                    "claim": claim_id,
                    "chunk": knowledge_chunk_id,
                    "url": item.canonical_url,
                    "excerpt": item.result.content[:1000],
                    "score": item.result.score,
                    "source_type": source_type,
                    "title": item.result.title[:300],
                    "organization": item.result.source_organization,
                    "domain": item.domain or item.result.source_organization,
                    "published": item.result.published_at,
                    "rank": item.index,
                },
            )
            citation_ids.append(str(citation_id))
            sources.append(
                {
                    "citation_id": str(citation_id),
                    "index": item.index,
                    "title": item.result.title,
                    "domain": item.domain or item.result.source_organization,
                    "source_type": source_type,
                    "source_level": item.source_level,
                    "published_at": item.result.published_at.isoformat()
                    if item.result.published_at
                    else None,
                }
            )
        return {
            "schema_version": "1.0",
            "card_id": str(uuid4()),
            "card_type": "SourceCard",
            "entity_refs": {"citation_ids": citation_ids},
            "payload": {"title": "参考来源", "sources": sources},
            "actions": [
                {
                    "action_id": f"open-source-{source['index']}",
                    "action_type": "OPEN_SOURCE",
                    "label": f"查看来源 {source['index']}",
                    "citation_id": source["citation_id"],
                }
                for source in sources
            ],
        }

    async def _regenerate_approval(
        self, connection: AsyncConnection, run: dict[str, Any], approval: dict[str, Any]
    ) -> None:
        items = (
            (
                await connection.execute(
                    text("SELECT * FROM approval_decision_items WHERE approval_id=:id"),
                    {"id": approval["id"]},
                )
            )
            .mappings()
            .all()
        )
        reduced_weekly_minutes: list[int] = []
        for item in items:
            current_weekly_minutes = int(
                (
                    await connection.execute(
                        text("SELECT weekly_minutes FROM plan_revisions WHERE id=:id"),
                        {"id": item["plan_revision_id"]},
                    )
                ).scalar_one()
            )
            # “降低负荷”按当前草案递减，绝不能回到历史硬编码值而突破用户刚确认的上限。
            next_weekly_minutes = max(30, current_weekly_minutes * 5 // 6)
            reduced_weekly_minutes.append(next_weekly_minutes)
            await connection.execute(
                text(
                    "UPDATE plan_revisions SET objective_summary=:summary,weekly_minutes=:weekly_minutes,"
                    "change_reason=:reason WHERE id=:id"
                ),
                {
                    "summary": "已按反馈降低负荷的四周公务员备考计划",
                    "weekly_minutes": next_weekly_minutes,
                    "reason": approval["feedback"],
                    "id": item["plan_revision_id"],
                },
            )
        new_version = approval["approval_version"] + 1
        await connection.execute(
            text(
                "UPDATE approval_decisions SET status='PENDING',action=NULL,decided_at=NULL,"
                "approval_version=:version WHERE id=:id"
            ),
            {"version": new_version, "id": approval["id"]},
        )
        response = "我已根据反馈更新草案，请再次使用计划卡确认。"
        message = _row(
            await connection.execute(
                text("SELECT structured_content FROM messages WHERE id=:id FOR UPDATE"),
                {"id": run["response_message_id"]},
            )
        )
        structured = (message or {}).get("structured_content")
        cards = structured.get("cards", []) if isinstance(structured, dict) else []
        for card in cards:
            if not isinstance(card, dict) or card.get("card_type") != "PlanCard":
                continue
            refs = card.get("entity_refs")
            if not isinstance(refs, dict) or str(refs.get("approval_id")) != str(approval["id"]):
                continue
            refs["approval_version"] = new_version
            refs["approval_status"] = "PENDING"
            payload = card.get("payload")
            if isinstance(payload, dict):
                payload["objective_summary"] = "已按反馈降低负荷的四周公务员备考计划"
                payload["weekly_minutes"] = min(reduced_weekly_minutes)
        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,"
                "structured_content=CAST(:cards AS jsonb),updated_at=now() WHERE id=:id"
            ),
            {
                "content": response,
                "cards": _json(structured if isinstance(structured, dict) else {}),
                "id": run["response_message_id"],
            },
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='AWAITING_APPROVAL',updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "interrupt",
            {
                "type": "APPROVAL",
                "approval_id": str(approval["id"]),
                "approval_version": new_version,
            },
            run["attempt"],
        )

    async def _publish_revision(
        self, connection: AsyncConnection, user_id: UUID, item: dict[str, Any]
    ) -> None:
        DEFAULT_CAPABILITY_REGISTRY.require("plan_publish", "Executor", approved=True)
        DEFAULT_CAPABILITY_REGISTRY.require("task_materialize", "Executor", approved=True)
        revision = _row(
            await connection.execute(
                text("SELECT * FROM plan_revisions WHERE id=:id AND user_id=:user_id"),
                {"id": item["plan_revision_id"], "user_id": user_id},
            )
        )
        if revision is None:
            raise conflict("PLAN_REVISION_CONFLICT", "计划草案不存在")
        await connection.execute(
            text(
                "UPDATE plan_revisions SET status='SUPERSEDED' WHERE plan_id=:plan AND status='APPROVED'"
            ),
            {"plan": item["plan_id"]},
        )
        await connection.execute(
            text("UPDATE plan_revisions SET status='APPROVED',approved_at=now() WHERE id=:id"),
            {"id": revision["id"]},
        )
        await connection.execute(
            text(
                "UPDATE plans SET current_revision_id=:revision,status='ACTIVE',updated_at=now() WHERE id=:plan;"
            ),
            {"revision": revision["id"], "plan": item["plan_id"]},
        )
        await connection.execute(
            text(
                "UPDATE goals SET status='ACTIVE',updated_at=now() WHERE id=(SELECT goal_id FROM plans WHERE id=:plan)"
            ),
            {"plan": item["plan_id"]},
        )
        tasks = (revision["content"] or {}).get("tasks", [])
        for index, task in enumerate(tasks):
            task_id = uuid5(UUID(str(revision["id"])), f"task:{index}")
            scheduled = revision["start_date"] + timedelta(days=int(task["day_offset"]))
            await connection.execute(
                text(
                    "INSERT INTO tasks(id,user_id,plan_id,plan_revision_id,title,scheduled_date,expected_minutes) "
                    "VALUES(:id,:user_id,:plan,:revision,:title,:date,:minutes) ON CONFLICT(id) DO NOTHING"
                ),
                {
                    "id": task_id,
                    "user_id": user_id,
                    "plan": item["plan_id"],
                    "revision": revision["id"],
                    "title": task["title"],
                    "date": scheduled,
                    "minutes": task["minutes"],
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO notification_jobs(id,user_id,task_id,event_type,channel,scheduled_at,status,idempotency_key) "
                    "VALUES(:id,:user_id,:task,'TASK_REMINDER','IN_APP',:scheduled,'PENDING',:key) "
                    "ON CONFLICT(idempotency_key) DO NOTHING"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "task": task_id,
                    "scheduled": datetime.combine(scheduled, datetime.min.time(), UTC),
                    "key": f"task-reminder:{task_id}:in-app",
                },
            )

    async def _complete_run(
        self,
        connection: AsyncConnection,
        run: dict[str, Any],
        response: str,
        *,
        cards: list[dict[str, object]] | None = None,
    ) -> None:
        structured = {"cards": cards or []}
        await connection.execute(
            text(
                "UPDATE messages SET status='COMPLETED',content=:content,"
                "structured_content=CAST(:structured AS jsonb),updated_at=now() WHERE id=:id"
            ),
            {
                "content": response,
                "structured": _json(structured),
                "id": run["response_message_id"],
            },
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='SUCCEEDED',updated_at=now() WHERE id=:id"),
            {"id": run["id"]},
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "message.completed",
            {
                "message_id": str(run["response_message_id"]),
                "content": response,
                "cards": cards or [],
            },
            run["attempt"],
        )
        await self._append_event(
            connection,
            run["id"],
            run["user_id"],
            "run.completed",
            {"status": "SUCCEEDED"},
            run["attempt"],
        )
        await self._maybe_archive_segment(connection, run)

    async def _fail_run(self, run_id: UUID, error: ButlerError) -> None:
        async with self.database.transaction() as connection:
            run = _row(
                await connection.execute(
                    text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
                )
            )
            if run is None:
                return
            status = "FAILED_RETRYABLE" if error.retryable else "FAILED_FINAL"
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status=:status,error_code=:code,updated_at=now() WHERE id=:id"
                ),
                {"status": status, "code": error.code, "id": run_id},
            )
            await connection.execute(
                text("UPDATE messages SET status='FAILED',updated_at=now() WHERE id=:id"),
                {"id": run["response_message_id"]},
            )
            await self._append_event(
                connection,
                run_id,
                run["user_id"],
                "error",
                {"code": error.code, "message": error.message, "retryable": error.retryable},
                run["attempt"],
            )

    async def _maybe_archive_segment(
        self, connection: AsyncConnection, run: dict[str, Any]
    ) -> None:
        total_chars = int(
            (
                await connection.execute(
                    text(
                        "SELECT COALESCE(SUM(length(content)),0) FROM messages WHERE segment_id=:segment"
                    ),
                    {"segment": run["segment_id"]},
                )
            ).scalar_one()
        )
        estimated = max(1, total_chars // 2)
        await connection.execute(
            text("UPDATE conversation_segments SET estimated_tokens=:tokens WHERE id=:id"),
            {"tokens": estimated, "id": run["segment_id"]},
        )
        hard = int(self.settings.context_window_tokens * self.settings.context_hard_limit_ratio)
        soft = int(self.settings.context_window_tokens * self.settings.context_soft_limit_ratio)
        if estimated >= soft:
            await connection.execute(
                text(
                    "INSERT INTO conversation_summaries(id,conversation_id,segment_id,summary_type,version,content,"
                    "source_message_count,token_count) VALUES(:id,:conversation,:segment,'INCREMENTAL',1,"
                    "CAST(:content AS jsonb),(SELECT COUNT(*) FROM messages WHERE segment_id=:segment),:tokens) "
                    "ON CONFLICT(conversation_id,segment_id,summary_type,version) DO UPDATE SET content=EXCLUDED.content,"
                    "source_message_count=EXCLUDED.source_message_count,token_count=EXCLUDED.token_count"
                ),
                {
                    "id": uuid4(),
                    "conversation": run["conversation_id"],
                    "segment": run["segment_id"],
                    "content": _json(
                        {"summary": "验证版确定性摘要", "source_segment_id": str(run["segment_id"])}
                    ),
                    "tokens": min(1500, estimated // 10),
                },
            )
        if estimated < hard:
            return
        conversation = _row(
            await connection.execute(
                text("SELECT * FROM conversations WHERE id=:id FOR UPDATE"),
                {"id": run["conversation_id"]},
            )
        )
        if conversation is None or conversation["active_segment_id"] != run["segment_id"]:
            return
        new_segment = uuid4()
        new_sequence = conversation["context_version"] + 1
        await connection.execute(
            text(
                "UPDATE conversation_segments SET status='ARCHIVED',archived_at=now() WHERE id=:id"
            ),
            {"id": run["segment_id"]},
        )
        await connection.execute(
            text(
                "INSERT INTO conversation_segments(id,conversation_id,user_id,sequence,thread_id,status) "
                "VALUES(:id,:conversation,:user_id,:sequence,:thread,'ACTIVE')"
            ),
            {
                "id": new_segment,
                "conversation": run["conversation_id"],
                "user_id": run["user_id"],
                "sequence": new_sequence,
                "thread": f"thread-{uuid4()}",
            },
        )
        await connection.execute(
            text(
                "UPDATE conversations SET active_segment_id=:segment,context_version=:version,updated_at=now() WHERE id=:id"
            ),
            {"segment": new_segment, "version": new_sequence, "id": run["conversation_id"]},
        )

    async def _append_event(
        self,
        connection: AsyncConnection,
        run_id: UUID,
        user_id: UUID,
        event_type: str,
        payload: dict[str, object],
        attempt: int,
    ) -> int:
        """在同一事务原子分配 sequence 并插入持久化事件。"""

        sequence = int(
            (
                await connection.execute(
                    text(
                        "UPDATE agent_runs SET last_event_sequence=last_event_sequence+1 "
                        "WHERE id=:id RETURNING last_event_sequence"
                    ),
                    {"id": run_id},
                )
            ).scalar_one()
        )
        await connection.execute(
            text(
                "INSERT INTO agent_run_events(id,run_id,user_id,sequence,event_type,attempt,payload) "
                "VALUES(:id,:run_id,:user_id,:sequence,:event_type,:attempt,CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "run_id": run_id,
                "user_id": user_id,
                "sequence": sequence,
                "event_type": event_type,
                "attempt": attempt,
                "payload": _json(payload),
            },
        )
        return sequence

    async def _ensure_agent_definitions(self, connection: AsyncConnection) -> None:
        catalog: tuple[tuple[Any, ...], ...] = (
            (
                BUTLER_DEFINITION_ID,
                "BUTLER",
                "AI 大管家",
                "butler_graph_v1",
                "HIDDEN",
                0,
                {},
            ),
            (
                CIVIL_DEFINITION_ID,
                "CIVIL_SERVICE_EXAM",
                "考公",
                "civil_service_v1",
                "AVAILABLE",
                10,
                {
                    "icon": "公",
                    "welcome_message": "我是你的考公助理，可以帮你制定备考计划、安排每日任务、整理资料并复盘错题。告诉我你的目标考试和可投入时间，我们从最重要的一步开始。",
                    "starter_prompts": [
                        {
                            "label": "制定备考计划",
                            "content": "根据我的目标考试和可投入时间，帮我制定备考计划",
                        },
                        {
                            "label": "安排今日任务",
                            "content": "结合我的备考进度，帮我安排今天的学习任务",
                        },
                        {
                            "label": "复盘错题",
                            "content": "帮我整理并复盘最近的错题，找出薄弱环节",
                        },
                    ],
                },
            ),
            (
                IELTS_DEFINITION_ID,
                "IELTS",
                "考雅思",
                "unavailable",
                "COMING_SOON",
                20,
                {"icon": "雅", "welcome_message": "", "starter_prompts": []},
            ),
            (
                JOB_SEARCH_DEFINITION_ID,
                "JOB_SEARCH",
                "求职",
                "unavailable",
                "COMING_SOON",
                30,
                {"icon": "职", "welcome_message": "", "starter_prompts": []},
            ),
        )
        for definition_id, code, name, graph, catalog_status, order, metadata in catalog:
            execution_status = "ACTIVE" if catalog_status != "COMING_SOON" else "DRAFT"
            await connection.execute(
                text(
                    "INSERT INTO agent_definitions(id,code,version,name,description,graph_name,status,"
                    "catalog_status,display_order,catalog_metadata) "
                    "VALUES(:id,:code,1,:name,:description,:graph,:status,:catalog_status,:display_order,"
                    "CAST(:metadata AS jsonb)) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,"
                    "description=EXCLUDED.description,catalog_status=EXCLUDED.catalog_status,"
                    "display_order=EXCLUDED.display_order,catalog_metadata=EXCLUDED.catalog_metadata"
                ),
                {
                    "id": definition_id,
                    "code": code,
                    "name": name,
                    "description": {
                        "CIVIL_SERVICE_EXAM": "规划备考与任务复盘",
                        "IELTS": "听说读写专项提升",
                        "JOB_SEARCH": "简历、面试与投递规划",
                    }.get(code, ""),
                    "graph": graph,
                    "status": execution_status,
                    "catalog_status": catalog_status,
                    "display_order": order,
                    "metadata": _json(metadata),
                },
            )

    async def _ensure_user_workspace(self, connection: AsyncConnection, user_id: UUID) -> None:
        butler_user_agent = uuid5(user_id, "BUTLER")
        civil_user_agent = uuid5(user_id, "CIVIL_SERVICE_EXAM")
        for user_agent_id, definition_id in (
            (butler_user_agent, BUTLER_DEFINITION_ID),
            (civil_user_agent, CIVIL_DEFINITION_ID),
        ):
            await connection.execute(
                text(
                    "INSERT INTO user_agents(id,user_id,agent_definition_id,status,activated_at) "
                    "VALUES(:id,:user_id,:definition,'ACTIVE',now()) ON CONFLICT(user_id,agent_definition_id) DO NOTHING"
                ),
                {"id": user_agent_id, "user_id": user_id, "definition": definition_id},
            )
        conversation_id = uuid5(user_id, "MAIN_CONVERSATION")
        segment_id = uuid5(user_id, "MAIN_CONVERSATION:1")
        await connection.execute(
            text(
                "INSERT INTO conversations(id,user_id,user_agent_id,client_conversation_id,title,status) "
                "VALUES(:id,:user_id,:agent,:id,'新的对话','CURRENT') "
                "ON CONFLICT(user_id,client_conversation_id) DO NOTHING"
            ),
            {"id": conversation_id, "user_id": user_id, "agent": butler_user_agent},
        )
        await connection.execute(
            text(
                "INSERT INTO conversation_segments(id,conversation_id,user_id,sequence,thread_id,status) "
                "VALUES(:id,:conversation,:user_id,1,:thread,'ACTIVE') ON CONFLICT(id) DO NOTHING"
            ),
            {
                "id": segment_id,
                "conversation": conversation_id,
                "user_id": user_id,
                "thread": f"thread-{segment_id}",
            },
        )
        await connection.execute(
            text(
                "UPDATE conversations SET active_segment_id=COALESCE(active_segment_id,:segment) WHERE id=:id"
            ),
            {"segment": segment_id, "id": conversation_id},
        )

    async def _ensure_synthetic_source(self, connection: AsyncConnection) -> None:
        digest = hashlib.sha256(b"synthetic civil service verification source").hexdigest()
        await connection.execute(
            text(
                "INSERT INTO knowledge_documents(id,visibility,domain,title,source_organization,source_level,sha256,"
                "retrieved_at,ingestion_status) VALUES(:id,'PUBLIC','CIVIL_SERVICE_EXAM',"
                "'合成公考验收资料（非真实公告）','AI Butler Test Fixtures','GENERAL',:sha,now(),'READY') "
                "ON CONFLICT(id) DO NOTHING"
            ),
            {"id": PUBLIC_SOURCE_ID, "sha": digest},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_chunks(id,document_id,chunk_index,content,token_count,content_hash,embedding_model,"
                "qdrant_collection,qdrant_point_id,vector_status) VALUES(:id,:document,0,"
                "'合成验收资料：训练计划包含行测与申论模块。',20,:hash,'fake-embedding-v1',"
                "'ai_butler_knowledge',:point,'READY') ON CONFLICT(id) DO NOTHING"
            ),
            {
                "id": PUBLIC_CHUNK_ID,
                "document": PUBLIC_SOURCE_ID,
                "hash": digest,
                "point": PUBLIC_CHUNK_ID,
            },
        )

    async def _get_user(self, connection: AsyncConnection, user_id: UUID) -> dict[str, object]:
        row = _row(
            await connection.execute(
                text(
                    "SELECT id,nickname,locale,timezone,status,created_at FROM users WHERE id=:id"
                ),
                {"id": user_id},
            )
        )
        if row is None:
            raise not_found()
        row["avatar_url"] = None
        return row

    async def _lock_user_and_check_global_run(
        self, connection: AsyncConnection, user_id: UUID
    ) -> None:
        """锁定用户级会话切换边界，并拒绝存在非终态 run 时的新建/恢复。"""

        owner = _row(
            await connection.execute(
                text("SELECT id FROM users WHERE id=:user_id FOR UPDATE"), {"user_id": user_id}
            )
        )
        if owner is None:
            raise not_found()
        active = _row(
            await connection.execute(
                text(
                    f"SELECT id,conversation_id,status FROM agent_runs WHERE user_id=:user_id "  # noqa: S608
                    f"AND status IN ({NON_TERMINAL_RUN_SQL}) FOR UPDATE"
                ),
                {"user_id": user_id},
            )
        )
        if active:
            raise ButlerError(
                "GLOBAL_RUN_IN_PROGRESS",
                "当前有对话正在处理中",
                409,
                details={
                    "run_id": str(active["id"]),
                    "conversation_id": str(active["conversation_id"]),
                },
            )

    async def _resolve_specialist(
        self, connection: AsyncConnection, user_id: UUID, specialist_code: str | None
    ) -> dict[str, Any] | None:
        """将公开 Agent code 解析为当前用户实例；未开放入口失败关闭。"""

        if specialist_code is None:
            return None
        row = _row(
            await connection.execute(
                text(
                    "SELECT ua.id AS user_agent_id,ad.code,ad.name,ad.catalog_status,"
                    "ad.catalog_metadata FROM agent_definitions ad "
                    "LEFT JOIN user_agents ua ON ua.agent_definition_id=ad.id "
                    "AND ua.user_id=:user_id AND ua.status='ACTIVE' "
                    "WHERE ad.code=:code ORDER BY ad.version DESC LIMIT 1"
                ),
                {"user_id": user_id, "code": specialist_code},
            )
        )
        if row is None or row["catalog_status"] != "AVAILABLE" or row["user_agent_id"] is None:
            raise conflict("AGENT_NOT_AVAILABLE", "该专业助理尚未开放")
        metadata = row["catalog_metadata"] if isinstance(row["catalog_metadata"], dict) else {}
        row["icon"] = str(metadata.get("icon", "AI"))
        row["welcome_message"] = str(metadata.get("welcome_message", ""))
        return row

    async def _conversation_row(
        self, connection: AsyncConnection, user_id: UUID, conversation_id: UUID
    ) -> dict[str, Any] | None:
        return _row(
            await connection.execute(
                text(
                    "SELECT c.*,ad.code AS specialist_code,ad.name AS specialist_name,"  # noqa: S608
                    "ad.catalog_metadata AS specialist_metadata,r.id AS active_run_id,"
                    "r.status AS active_run_status,lm.content AS last_message_content,"
                    "lm.created_at AS last_message_created_at FROM conversations c "
                    "LEFT JOIN user_agents ua ON ua.id=c.specialist_user_agent_id "
                    "LEFT JOIN agent_definitions ad ON ad.id=ua.agent_definition_id "
                    f"LEFT JOIN agent_runs r ON r.conversation_id=c.id AND r.status IN ({NON_TERMINAL_RUN_SQL}) "
                    "LEFT JOIN LATERAL (SELECT content,created_at FROM messages "
                    "WHERE conversation_id=c.id AND role IN ('USER','ASSISTANT') "
                    "ORDER BY created_at DESC,id DESC LIMIT 1) lm ON true "
                    "WHERE c.id=:conversation_id AND c.user_id=:user_id"
                ),
                {"conversation_id": conversation_id, "user_id": user_id},
            )
        )

    async def _hydrate_approval_cards(
        self,
        connection: AsyncConnection,
        user_id: UUID,
        messages: list[dict[str, Any]],
    ) -> None:
        """用审批事实覆盖消息快照中的可变版本与草案摘要。

        PlanCard 是历史消息的一部分，但 EDIT 会在同一 approval 上生成新版本。
        读取时必须投影当前版本，既修复旧数据，也避免客户端刷新后继续提交旧版本。
        """

        approval_ids: set[UUID] = set()
        for message in messages:
            structured = message.get("cards")
            cards = structured.get("cards", []) if isinstance(structured, dict) else []
            for card in cards:
                if not isinstance(card, dict) or card.get("card_type") != "PlanCard":
                    continue
                refs = card.get("entity_refs")
                if not isinstance(refs, dict) or not refs.get("approval_id"):
                    continue
                try:
                    approval_ids.add(UUID(str(refs["approval_id"])))
                except ValueError:
                    continue
        if not approval_ids:
            return
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT DISTINCT ON (a.id) a.id,a.approval_version,a.status,"
                        "pr.objective_summary,pr.weekly_minutes FROM approval_decisions a "
                        "JOIN approval_decision_items ai ON ai.approval_id=a.id "
                        "JOIN plan_revisions pr ON pr.id=ai.plan_revision_id "
                        "WHERE a.user_id=:user_id AND a.id=ANY(:approval_ids) "
                        "ORDER BY a.id,ai.id"
                    ),
                    {"user_id": user_id, "approval_ids": list(approval_ids)},
                )
            )
            .mappings()
            .all()
        )
        current = {str(row["id"]): row for row in rows}
        for message in messages:
            structured = message.get("cards")
            cards = structured.get("cards", []) if isinstance(structured, dict) else []
            for card in cards:
                if not isinstance(card, dict) or card.get("card_type") != "PlanCard":
                    continue
                refs = card.get("entity_refs")
                if not isinstance(refs, dict):
                    continue
                approval = current.get(str(refs.get("approval_id")))
                if approval is None:
                    continue
                refs["approval_version"] = int(approval["approval_version"])
                refs["approval_status"] = str(approval["status"])
                payload = card.get("payload")
                if isinstance(payload, dict):
                    payload["objective_summary"] = approval["objective_summary"]
                    payload["weekly_minutes"] = int(approval["weekly_minutes"])

    @staticmethod
    def _specialist_response(row: dict[str, Any] | None) -> dict[str, object] | None:
        if row is None:
            return None
        return {"code": row["code"], "name": row["name"], "icon": row["icon"]}

    def _conversation_response(self, row: dict[str, Any]) -> dict[str, object]:
        raw_metadata = row.get("specialist_metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        specialist = None
        if row.get("specialist_code"):
            specialist = {
                "code": row["specialist_code"],
                "name": row["specialist_name"],
                "icon": str(metadata.get("icon", "AI")),
            }
        active_run = None
        if row.get("active_run_id"):
            active_run = {"id": row["active_run_id"], "status": row["active_run_status"]}
        last_message = None
        if row.get("last_message_created_at"):
            last_message = {
                "content": str(row.get("last_message_content", ""))[:120],
                "created_at": row["last_message_created_at"],
            }
        return {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "specialist": specialist,
            "last_message": last_message,
            "last_message_at": row.get("last_message_at"),
            "active_run": active_run,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _token_response(
        self,
        user: dict[str, object],
        session_id: UUID,
        refresh_token: str,
        is_new: bool,
    ) -> dict[str, object]:
        user = {**user, "is_new_user": is_new}
        return {
            "access_token": issue_access_token(
                UUID(str(user["id"])),
                session_id,
                self.settings.auth_access_token_secret,
                self.settings.auth_access_token_seconds,
            ),
            "token_type": "Bearer",
            "expires_in": self.settings.auth_access_token_seconds,
            "refresh_token": refresh_token,
            "refresh_expires_in": self.settings.auth_refresh_token_seconds,
            "user": user,
        }

    def _send_response(
        self, conversation: dict[str, Any], run: dict[str, Any]
    ) -> dict[str, object]:
        run_id = UUID(str(run["run_id"]))
        return {
            "schema_version": "1.0",
            "conversation_id": conversation["id"],
            "user_message": {"id": run["user_message_id"], "status": "COMPLETED"},
            "assistant_message": {"id": run["response_message_id"], "status": "PENDING"},
            "run": {
                "id": run_id,
                "status": run["status"],
                "execution_mode": "START",
                "attempt": run["attempt"],
            },
            "stream": {
                "events_url": f"/v1/agent-runs/{run_id}/events",
                "ticket": issue_signed_ticket(
                    run_id,
                    self.settings.stream_ticket_secret,
                    self.settings.stream_ticket_seconds,
                ),
                "expires_at": datetime.now(UTC)
                + timedelta(seconds=self.settings.stream_ticket_seconds),
                "last_sequence": 1,
            },
        }

    @staticmethod
    def _build_search_provider(settings: Settings) -> SearchProvider:
        """根据类型化配置构建搜索适配器；未知供应商在启动时失败关闭。"""

        if settings.search_provider == "fake":
            return FakeSearchProvider()
        if settings.search_provider == "tavily":
            return TavilySearchProvider(
                settings.tavily_api_key,
                settings.tavily_base_url,
                settings.search_timeout_seconds,
            )
        raise ValueError(f"unsupported search provider: {settings.search_provider}")

    @staticmethod
    def _build_embedding_provider(settings: Settings) -> EmbeddingProvider:
        """构建私有资料 Embedding；Fake 模式固定、无费用且完全确定性。"""

        if settings.embedding_model == "fake-embedding-v1":
            return FakeEmbeddingProvider()
        return OpenAICompatibleEmbeddingProvider(
            settings.llm_api_key,
            settings.llm_base_url,
            settings.embedding_model,
        )

    @staticmethod
    def _build_llm(settings: Settings) -> LLM:
        """构建时间提取模型；未知供应商在启动阶段失败，避免静默降级。"""

        if settings.llm_provider == "fake":
            return FakeLLM(settings.chat_model)
        if settings.llm_provider == "openai-compatible":
            return OpenAICompatibleLLM(
                settings.llm_api_key,
                settings.llm_base_url,
                settings.chat_model,
            )
        raise ValueError(f"unsupported llm provider: {settings.llm_provider}")

    @staticmethod
    def _safe_summary(content: str) -> str:
        normalized = " ".join(content.split())
        return f"用户提交了 {len(normalized)} 个字符的请求"

    @staticmethod
    def _draft_tasks_for_availability(
        start: date, availability: AvailabilityInterpretationV1
    ) -> list[dict[str, object]]:
        """把验证版任务放入允许日期，并将单项时长限制在当天可用容量内。"""

        capacity_by_day: dict[int, int] = {}
        for window in availability.windows:
            capacity_by_day[window.day_of_week] = (
                capacity_by_day.get(window.day_of_week, 0) + window.available_minutes
            )
        allowed_days = (
            set(capacity_by_day)
            if capacity_by_day
            else set(range(1, 8)) - set(availability.excluded_days)
        )
        templates = (
            ("行测基础摸底", 40),
            ("申论素材精读", 30),
            ("错题复盘", 35),
        )
        tasks: list[dict[str, object]] = []
        next_offset = 0
        for title, expected_minutes in templates:
            while (
                next_offset <= 27
                and (start + timedelta(days=next_offset)).isoweekday() not in allowed_days
            ):
                next_offset += 1
            if next_offset > 27:
                break
            weekday = (start + timedelta(days=next_offset)).isoweekday()
            available_minutes = capacity_by_day.get(weekday, expected_minutes)
            tasks.append(
                {
                    "title": title,
                    "day_offset": next_offset,
                    "minutes": min(expected_minutes, available_minutes),
                }
            )
            next_offset += 1
        return tasks

    @staticmethod
    def _validate_availability_overlap(request: AvailabilityRequest) -> None:
        for index, left in enumerate(request.windows):
            for right in request.windows[index + 1 :]:
                if left.day_of_week != right.day_of_week:
                    continue
                if left.start_time is None or right.start_time is None:
                    raise ButlerError("AVAILABILITY_OVERLAP", "学习时间配置存在重复默认项", 400)
                if left.start_time < right.end_time and right.start_time < left.end_time:  # type: ignore[operator]
                    raise ButlerError("AVAILABILITY_OVERLAP", "学习时间窗口不能重叠", 400)
