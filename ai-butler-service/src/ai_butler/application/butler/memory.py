"""用户长期记忆服务；正文仅进入 LangGraph PostgresStore，业务库只留哈希审计。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from langgraph.store.postgres.aio import AsyncPostgresStore
from sqlalchemy import text

from ai_butler.agent.runtime import MemoryCandidate, MemoryPolicy

from .context import ButlerContext

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


@dataclass(frozen=True, slots=True)
class MemoryCommandResult:
    handled: bool
    response: str


class LongTermMemoryService:
    """按用户 namespace 串行执行记忆写入、更正、遗忘和检索。"""

    def __init__(self, context: ButlerContext) -> None:
        self.database = context.database
        self.settings = context.settings
        self._database_url = context.settings.langgraph_database_url
        self._policy = MemoryPolicy()

    async def handle_explicit_command(
        self, user_id: UUID, content: str
    ) -> MemoryCommandResult | None:
        normalized = content.strip()
        if normalized in {"暂停记忆", "停止记忆", "不要再记忆"}:
            await self._set_enabled(user_id, False)
            return MemoryCommandResult(True, "已暂停长期记忆。之后的内容不会自动写入记忆。")
        if normalized in {"恢复记忆", "继续记忆"}:
            await self._set_enabled(user_id, True)
            return MemoryCommandResult(True, "已恢复长期记忆。敏感和临时信息仍不会被保存。")
        correction = CORRECT_PATTERN.fullmatch(normalized)
        if correction:
            await self.forget(user_id, correction.group(1), "CORRECTED")
            admitted = await self.remember(user_id, correction.group(2), user_requested=True)
            return MemoryCommandResult(
                True,
                "已更正这条记忆。" if admitted else "旧记忆已忘记，但新内容不符合记忆安全策略。",
            )
        forgetting = FORGET_PATTERN.fullmatch(normalized)
        if forgetting:
            if forgetting.group(1).strip() in {"全部", "所有", "全部记忆", "所有记忆"}:
                await self.forget_all(user_id)
                return MemoryCommandResult(True, "已忘记全部长期记忆，并为旧值写入遗忘屏障。")
            await self.forget(user_id, forgetting.group(1), "USER_REQUESTED")
            return MemoryCommandResult(True, "已忘记这条长期记忆，并记录防止自动重新写入。")
        remembering = REMEMBER_PATTERN.fullmatch(normalized)
        if remembering:
            admitted = await self.remember(user_id, remembering.group(1), user_requested=True)
            return MemoryCommandResult(
                True,
                "好的，我会记住。"
                if admitted
                else "这类敏感、临时或业务实体信息不会写入长期记忆。",
            )
        return None

    async def remember(self, user_id: UUID, value: str, *, user_requested: bool) -> bool:
        normalized_value = self._normalize(value)
        key = self._memory_key(normalized_value)
        category = self._category(normalized_value)
        candidate = MemoryCandidate(
            normalized_key=key,
            value=normalized_value,
            category=category,
            explicit=1.0 if user_requested else 0.7,
            stable=0.9,
            useful=0.8,
            specific=0.8,
            repeated=0.5,
            user_requested=user_requested,
            sensitive=bool(
                SENSITIVE_PATTERN.search(normalized_value)
                or TEMPORARY_PATTERN.search(normalized_value)
                or BUSINESS_ENTITY_PATTERN.search(normalized_value)
            ),
        )
        admitted, ttl_days = self._policy.admit(candidate)
        if not admitted or ttl_days is None or not await self._enabled(user_id):
            await self._audit(user_id, "REJECTED", key)
            return False
        async with self.database.transaction() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:user_id,0))"),
                {"user_id": str(user_id)},
            )
            tombstoned = (
                await connection.execute(
                    text(
                        "SELECT 1 FROM memory_tombstones WHERE user_id=:user_id "
                        "AND normalized_key=:key"
                    ),
                    {"user_id": user_id, "key": key},
                )
            ).scalar_one_or_none()
            if tombstoned:
                return False
        async with AsyncPostgresStore.from_conn_string(
            self._database_url,
            ttl={"refresh_on_read": False, "sweep_interval_minutes": 60},
        ) as store:
            await store.aput(
                self._namespace(user_id),
                key,
                {"value": normalized_value, "category": candidate.category},
                ttl=float(ttl_days * 24 * 60),
            )
        await self._audit(user_id, "REMEMBERED", key)
        return True

    async def extract_automatic(self, user_id: UUID, content: str) -> bool:
        """确定性提取稳定候选；模型候选接入后仍必须经过同一 Policy。"""

        match = AUTOMATIC_PATTERN.search(content.strip())
        if match is None:
            return False
        return await self.remember(user_id, match.group(0), user_requested=False)

    async def forget(self, user_id: UUID, value: str, reason: str) -> None:
        key = self._memory_key(self._normalize(value))
        async with self.database.transaction() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:user_id,0))"),
                {"user_id": str(user_id)},
            )
            await connection.execute(
                text(
                    "INSERT INTO memory_tombstones(id,user_id,normalized_key,reason) "
                    "VALUES(:id,:user_id,:key,:reason) ON CONFLICT(user_id,normalized_key) "
                    "DO UPDATE SET reason=EXCLUDED.reason,created_at=now()"
                ),
                {"id": uuid4(), "user_id": user_id, "key": key, "reason": reason},
            )
        async with AsyncPostgresStore.from_conn_string(
            self._database_url,
            ttl={"refresh_on_read": False, "sweep_interval_minutes": 60},
        ) as store:
            await store.adelete(self._namespace(user_id), key)
        await self._audit(user_id, "FORGOTTEN", key)

    async def forget_all(self, user_id: UUID) -> None:
        namespace = self._namespace(user_id)
        async with AsyncPostgresStore.from_conn_string(
            self._database_url,
            ttl={"refresh_on_read": False, "sweep_interval_minutes": 60},
        ) as store:
            items = await store.asearch(namespace, limit=1000, refresh_ttl=False)
        for item in items:
            value_map = getattr(item, "value", {})
            if isinstance(value_map, dict) and value_map.get("value"):
                await self.forget(user_id, str(value_map["value"]), "USER_REQUESTED_ALL")

    async def search(self, user_id: UUID, query: str) -> tuple[str, ...]:
        """Response 等后置节点最多取 8 条/800 Token；Router 不调用本方法。"""

        if not await self._enabled(user_id):
            return ()
        async with AsyncPostgresStore.from_conn_string(
            self._database_url,
            ttl={"refresh_on_read": False, "sweep_interval_minutes": 60},
        ) as store:
            items = await store.asearch(self._namespace(user_id), limit=50, refresh_ttl=False)
        query_terms = set(self._normalize(query))
        ranked: list[tuple[int, str]] = []
        for item in items:
            value_map = getattr(item, "value", {})
            value = str(value_map.get("value", "")) if isinstance(value_map, dict) else ""
            if value:
                ranked.append((len(query_terms & set(value)), value))
        selected: list[str] = []
        token_count = 0
        for _, value in sorted(ranked, key=lambda pair: pair[0], reverse=True):
            estimated = max(1, len(value) // 2)
            if token_count + estimated > 800:
                continue
            selected.append(value)
            token_count += estimated
            if len(selected) == 8:
                break
        return tuple(selected)

    async def _set_enabled(self, user_id: UUID, enabled: bool) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO memory_policy_state(user_id,enabled) VALUES(:user_id,:enabled) "
                    "ON CONFLICT(user_id) DO UPDATE SET enabled=EXCLUDED.enabled,"
                    "version=memory_policy_state.version+1,updated_at=now()"
                ),
                {"user_id": user_id, "enabled": enabled},
            )

    async def _enabled(self, user_id: UUID) -> bool:
        async with self.database.connect() as connection:
            value = (
                await connection.execute(
                    text("SELECT enabled FROM memory_policy_state WHERE user_id=:user_id"),
                    {"user_id": user_id},
                )
            ).scalar_one_or_none()
        return True if value is None else bool(value)

    async def _audit(self, user_id: UUID, action: str, key: str) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO memory_audit_records(id,user_id,action,memory_key_hash,metadata) "
                    "VALUES(:id,:user_id,:action,:key,'{}')"
                ),
                {"id": uuid4(), "user_id": user_id, "action": action, "key": key},
            )

    @staticmethod
    def _namespace(user_id: UUID) -> tuple[str, ...]:
        return ("users", str(user_id), "long_term_memory")

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip())[:500]

    @staticmethod
    def _memory_key(value: str) -> str:
        return hashlib.sha256(value.casefold().encode()).hexdigest()

    @staticmethod
    def _category(
        value: str,
    ) -> Literal["PREFERENCE", "HABIT", "CONSTRAINT", "BACKGROUND"]:
        if re.search(r"(?:必须|不能|约束|限制)", value):
            return "CONSTRAINT"
        if re.search(r"(?:背景|学历|职业|专业)", value):
            return "BACKGROUND"
        if re.search(r"(?:习惯|通常|经常)", value):
            return "HABIT"
        return "PREFERENCE"
