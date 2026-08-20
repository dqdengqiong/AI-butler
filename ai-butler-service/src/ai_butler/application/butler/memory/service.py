"""三阶段长期记忆服务：业务控制面 + LangGraph PostgresStore 正文/向量。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text

from ai_butler.agent.evidence import estimate_tokens
from ai_butler.domain.errors import ButlerError

from ..context import ButlerContext
from .consistency import MemoryConsistencyMixin
from .policy import MemoryCandidate, MemoryPolicy
from .store import store_index_config

REMEMBER_PATTERN = re.compile(r"(?:请)?记住[：:\s]*(.+)")
FORGET_PATTERN = re.compile(r"(?:请)?忘记[：:\s]*(.+)")
CORRECT_PATTERN = re.compile(r"纠正[：:\s]*(.+?)(?:为|改成)(.+)")
SENSITIVE_PATTERN = re.compile(
    r"(?:密码|口令|身份证|银行卡|信用卡|验证码|病历|诊断|手机号|手机号码|住址|家庭住址)"
    r"|(?:\b1[3-9]\d{9}\b)|(?:\b\d{15,18}[0-9Xx]\b)"
)
TEMPORARY_PATTERN = re.compile(r"(?:今天|这次|本次|暂时|当前|刚刚|稍后|明天)")
BUSINESS_ENTITY_PATTERN = re.compile(r"(?:plan_id|task_id|run_id|[0-9a-f]{8}-[0-9a-f-]{27})", re.I)
AUTOMATIC_PATTERN = re.compile(r"(?:我喜欢|我习惯|我通常|我的长期目标是)[：:\s]*(.+)")
SLOT_PREFIX_PATTERN = re.compile(
    r"^(?:我)?(?:喜欢|偏好|习惯|通常|经常|的长期目标是|长期目标是)[：:\s]*"
)


@dataclass(frozen=True, slots=True)
class MemoryCommandResult:
    handled: bool
    response: str


class LongTermMemoryService(MemoryConsistencyMixin):
    """Store 保存正文；业务库状态是检索、权限和遗忘的最终裁决。"""

    def __init__(self, context: ButlerContext) -> None:
        self.database = context.database
        self.settings = context.settings
        self._database_url = context.settings.langgraph_database_url
        self._index = store_index_config(context.embedding_provider)
        self._policy = MemoryPolicy(
            preference_ttl_days=context.settings.memory_preference_ttl_days,
            constraint_ttl_days=context.settings.memory_constraint_ttl_days,
        )

    async def handle_explicit_command(
        self, user_id: UUID, content: str
    ) -> MemoryCommandResult | None:
        normalized = content.strip()
        if normalized in {"暂停记忆", "停止记忆", "不要再记忆"}:
            await self._set_automatic_enabled(user_id, False)
            return MemoryCommandResult(True, "已暂停自动记忆；显式记住、查看和遗忘仍可使用。")
        if normalized in {"恢复记忆", "继续记忆"}:
            await self._set_automatic_enabled(user_id, True)
            return MemoryCommandResult(True, "已恢复自动记忆；暂停期间的消息不会回溯提取。")
        if normalized in {"查看记忆", "查看我的记忆", "你记得我什么", "你记得什么"}:
            try:
                memories = await self.search(user_id, "我的偏好、习惯、约束和背景")
            except Exception as exc:
                raise ButlerError(
                    "MEMORY_STORE_UNAVAILABLE", "长期记忆暂时不可用", 503, True
                ) from exc
            return MemoryCommandResult(
                True,
                "目前记得：" + "；".join(memories) if memories else "目前没有可用的长期记忆。",
            )
        correction = CORRECT_PATTERN.fullmatch(normalized)
        if correction:
            slot_key = await self.forget(user_id, correction.group(1), "CORRECTED")
            admitted = await self.remember(
                user_id,
                correction.group(2),
                user_requested=True,
                slot_key_override=slot_key,
                allow_tombstone_replacement=True,
            )
            return MemoryCommandResult(
                True,
                "已更正这条记忆。" if admitted else "旧记忆已忘记，但新内容不符合记忆策略。",
            )
        forgetting = FORGET_PATTERN.fullmatch(normalized)
        if forgetting:
            if forgetting.group(1).strip() in {"全部", "所有", "全部记忆", "所有记忆"}:
                await self.forget_all(user_id)
                return MemoryCommandResult(True, "已忘记全部长期记忆。")
            await self.forget(user_id, forgetting.group(1), "USER_REQUESTED")
            return MemoryCommandResult(True, "已忘记这条长期记忆。")
        remembering = REMEMBER_PATTERN.fullmatch(normalized)
        if remembering:
            admitted = await self.remember(user_id, remembering.group(1), user_requested=True)
            return MemoryCommandResult(
                True,
                "好的，我会记住。"
                if admitted
                else "这类敏感、临时、业务实体或已明确遗忘的内容不会写入长期记忆。",
            )
        return None

    async def remember(
        self,
        user_id: UUID,
        value: str,
        *,
        user_requested: bool,
        source_conversation_id: UUID | None = None,
        expected_generation: int | None = None,
        slot_key_override: str | None = None,
        allow_tombstone_replacement: bool = False,
    ) -> bool:
        statement = self._normalize(value)
        category = self._category(statement)
        slot_key = slot_key_override or self._slot_key(statement, category)
        slot_key_hash = self._hash(slot_key)
        candidate = MemoryCandidate(
            normalized_key=slot_key_hash,
            value=statement,
            category=category,
            explicit=1.0 if user_requested else 0.7,
            stable=0.9,
            useful=0.8,
            specific=0.8,
            repeated=0.5,
            user_requested=user_requested,
            sensitive=bool(
                SENSITIVE_PATTERN.search(statement)
                or TEMPORARY_PATTERN.search(statement)
                or BUSINESS_ENTITY_PATTERN.search(statement)
            ),
        )
        admitted, ttl_days = self._policy.admit(candidate)
        if not admitted:
            await self._audit(user_id, "REJECTED", slot_key_hash)
            return False
        pending = await self._begin_write(
            user_id,
            statement,
            slot_key,
            category,
            user_requested=user_requested,
            ttl_days=None if user_requested else ttl_days,
            source_conversation_id=source_conversation_id,
            expected_generation=expected_generation,
            allow_tombstone_replacement=allow_tombstone_replacement,
        )
        if pending is None:
            await self._audit(user_id, "REJECTED", slot_key_hash)
            return False
        now = datetime.now(UTC)
        value_map: dict[str, object] = {
            "schema_version": "2.0",
            "slot_key": pending.slot_key,
            "statement": statement,
            "category": category,
            "source_type": "EXPLICIT" if user_requested else "AUTOMATIC",
            "confidence": 1.0 if user_requested else 0.8,
            "stability": candidate.stable,
            "usefulness": candidate.useful,
            "occurrence_count": 1,
            "topic_tags": [],
            "revision": pending.revision,
            "policy_generation": pending.policy_generation,
            "created_at": now.isoformat(),
            "last_confirmed_at": now.isoformat(),
            "expires_at": pending.expires_at.isoformat() if pending.expires_at else None,
        }
        try:
            async with self._store() as store:
                await store.aput(
                    self._namespace(user_id),
                    pending.store_key,
                    value_map,
                    ttl=(
                        float(ttl_days * 24 * 60)
                        if not user_requested and ttl_days is not None
                        else None
                    ),
                )
        except Exception as exc:
            await self._invalidate_pending(user_id, pending, "STORE_WRITE_FAILED")
            raise ButlerError("MEMORY_STORE_UNAVAILABLE", "长期记忆暂时不可用", 503, True) from exc
        activated = await self._activate(user_id, pending)
        if not activated:
            if await self._safe_store_delete(user_id, pending.store_key):
                await self._mark_store_deleted(user_id, pending.store_key)
            return False
        await self._audit(user_id, "REMEMBERED", pending.store_key)
        return True

    async def extract_automatic(
        self,
        user_id: UUID,
        content: str,
        *,
        source_conversation_id: UUID | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        match = AUTOMATIC_PATTERN.search(content.strip())
        if match is None:
            return False
        return await self.remember(
            user_id,
            match.group(0),
            user_requested=False,
            source_conversation_id=source_conversation_id,
            expected_generation=expected_generation,
        )

    async def forget(self, user_id: UUID, value: str, reason: str) -> str:
        statement = self._normalize(value)
        store_key, slot_key = await self._resolve_forget_target(user_id, statement)
        slot_hash = self._hash(slot_key)
        async with self.database.transaction() as connection:
            await self._lock_user(connection, user_id)
            generation = await self._next_generation(connection, user_id)
            await connection.execute(
                text(
                    "UPDATE memory_control_records SET status='DELETED',updated_at=now() "
                    "WHERE user_id=:user_id AND store_key=:store_key "
                    "AND status IN ('PENDING','ACTIVE','CONFLICTED')"
                ),
                {"user_id": user_id, "store_key": store_key},
            )
            await connection.execute(
                text(
                    "INSERT INTO memory_tombstones(id,user_id,scope,slot_key_hash,statement_hash,"
                    "reason,policy_generation) VALUES(:id,:user_id,'SLOT',:slot_hash,:statement_hash,"
                    ":reason,:generation) ON CONFLICT(user_id,scope,slot_key_hash) DO UPDATE SET "
                    "statement_hash=EXCLUDED.statement_hash,reason=EXCLUDED.reason,"
                    "policy_generation=EXCLUDED.policy_generation,created_at=now()"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "slot_hash": slot_hash,
                    "statement_hash": self._hash(statement),
                    "reason": reason,
                    "generation": generation,
                },
            )
            await self._mark_profile_stale(connection, user_id, generation)
        if await self._safe_store_delete(user_id, store_key):
            await self._mark_store_deleted(user_id, store_key)
        await self._audit(user_id, "FORGOTTEN", store_key)
        return slot_key

    async def forget_all(self, user_id: UUID) -> None:
        async with self.database.transaction() as connection:
            await self._lock_user(connection, user_id)
            generation = await self._next_generation(connection, user_id, forget_all=True)
            store_keys = tuple(
                str(value)
                for value in (
                    await connection.execute(
                        text(
                            "UPDATE memory_control_records SET status='DELETED',updated_at=now() "
                            "WHERE user_id=:user_id AND status<>'DELETED' RETURNING store_key"
                        ),
                        {"user_id": user_id},
                    )
                ).scalars()
            )
            all_hash = self._hash("*")
            await connection.execute(
                text(
                    "INSERT INTO memory_tombstones(id,user_id,scope,slot_key_hash,reason,"
                    "policy_generation) VALUES(:id,:user_id,'USER',:slot_hash,'USER_REQUESTED_ALL',"
                    ":generation) ON CONFLICT(user_id,scope,slot_key_hash) DO UPDATE SET "
                    "policy_generation=EXCLUDED.policy_generation,created_at=now()"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "slot_hash": all_hash,
                    "generation": generation,
                },
            )
            await self._mark_profile_stale(connection, user_id, generation)
        for store_key in store_keys:
            if await self._safe_store_delete(user_id, store_key):
                await self._mark_store_deleted(user_id, store_key)
        await self._audit(user_id, "FORGOTTEN_ALL", self._hash("*"))

    async def search(self, user_id: UUID, query: str) -> tuple[str, ...]:
        async with self._store() as store:
            items = await store.asearch(
                self._namespace(user_id), query=query, limit=30, refresh_ttl=False
            )
        if not items:
            return ()
        keys = tuple(str(item.key) for item in items)
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT store_key,revision,source_type,category,policy_generation,updated_at "
                            "FROM memory_control_records WHERE user_id=:user_id "
                            "AND store_key=ANY(:keys) AND status='ACTIVE' "
                            "AND (expires_at IS NULL OR expires_at>now())"
                        ),
                        {"user_id": user_id, "keys": list(keys)},
                    )
                )
                .mappings()
                .all()
            )
        controls = {str(row["store_key"]): row for row in rows}
        ranked: list[tuple[float, str]] = []
        for item in items:
            value_map = item.value if isinstance(item.value, dict) else {}
            control = controls.get(str(item.key))
            statement = str(value_map.get("statement") or "")
            if (
                control is None
                or not statement
                or int(value_map.get("revision") or 0) != int(control["revision"])
                or int(value_map.get("policy_generation") or 0) != int(control["policy_generation"])
            ):
                continue
            semantic = float(getattr(item, "score", 0.0) or 0.0)
            explicit_bonus = 0.12 if control["source_type"] == "EXPLICIT" else 0.0
            confidence = float(value_map.get("confidence") or 0.0) * 0.08
            ranked.append((semantic + explicit_bonus + confidence, statement))
        selected: list[str] = []
        tokens = 0
        for _, statement in sorted(ranked, key=lambda pair: pair[0], reverse=True):
            item_tokens = estimate_tokens(statement)
            if tokens + item_tokens > 600:
                continue
            selected.append(statement)
            tokens += item_tokens
            if len(selected) == 4:
                break
        return tuple(selected)

    @staticmethod
    async def _mark_profile_stale(connection: object, user_id: UUID, generation: int) -> None:
        await connection.execute(  # type: ignore[attr-defined]
            text(
                "UPDATE user_profile_snapshots SET status='STALE',policy_generation=:generation,"
                "updated_at=now() WHERE user_id=:user_id"
            ),
            {"user_id": user_id, "generation": generation},
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip())[:500]

    @staticmethod
    def _slot_key(statement: str, category: str) -> str:
        subject = SLOT_PREFIX_PATTERN.sub("", statement.casefold()).strip(" ：:。.!！?")
        return f"{category.casefold()}:{subject}"

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.casefold().encode()).hexdigest()

    @staticmethod
    def _category(
        value: str,
    ) -> Literal["PREFERENCE", "HABIT", "CONSTRAINT", "BACKGROUND"]:
        if re.search(r"(?:必须|不能|约束|限制|长期目标)", value):
            return "CONSTRAINT"
        if re.search(r"(?:背景|学历|职业|专业)", value):
            return "BACKGROUND"
        if re.search(r"(?:习惯|通常|经常)", value):
            return "HABIT"
        return "PREFERENCE"
