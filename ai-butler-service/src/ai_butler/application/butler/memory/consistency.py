"""长期记忆控制面与 LangGraph Store 的一致性协议。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from langgraph.store.postgres.aio import AsyncPostgresStore
from sqlalchemy import text

from ai_butler.domain.errors import ButlerError

from ..shared import _json
from .store import STORE_TTL_CONFIG


@dataclass(frozen=True, slots=True)
class PendingMemoryWrite:
    store_key: str
    slot_key: str
    revision: int
    policy_generation: int
    expires_at: datetime | None


class MemoryConsistencyMixin:
    """要求宿主提供 database、Store 配置和稳定 slot/hash 工具。"""

    database: Any
    _database_url: str
    _index: Any

    def _hash(self, value: str) -> str:
        raise NotImplementedError

    def _slot_key(self, statement: str, category: str) -> str:
        raise NotImplementedError

    def _category(self, value: str) -> str:
        raise NotImplementedError

    async def _begin_write(
        self,
        user_id: UUID,
        statement: str,
        slot_key: str,
        category: str,
        *,
        user_requested: bool,
        ttl_days: int | None,
        source_conversation_id: UUID | None,
        expected_generation: int | None,
        allow_tombstone_replacement: bool,
    ) -> PendingMemoryWrite | None:
        store_key = self._hash(slot_key)
        expires_at = datetime.now(UTC) + timedelta(days=ttl_days) if ttl_days else None
        async with self.database.transaction() as connection:
            await self._lock_user(connection, user_id)
            policy = (
                (
                    await connection.execute(
                        text(
                            "SELECT automatic_enabled,policy_generation FROM memory_policy_state "
                            "WHERE user_id=:user_id FOR UPDATE"
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .one()
            )
            generation = int(policy["policy_generation"])
            if (not user_requested and not bool(policy["automatic_enabled"])) or (
                expected_generation is not None and expected_generation != generation
            ):
                return None
            tombstoned = (
                await connection.execute(
                    text(
                        "SELECT 1 FROM memory_tombstones WHERE user_id=:user_id "
                        "AND scope='SLOT' AND slot_key_hash=:slot_hash"
                    ),
                    {"user_id": user_id, "slot_hash": self._hash(slot_key)},
                )
            ).scalar_one_or_none()
            if tombstoned and not allow_tombstone_replacement:
                return None
            if allow_tombstone_replacement:
                await connection.execute(
                    text(
                        "DELETE FROM memory_tombstones WHERE user_id=:user_id "
                        "AND scope='SLOT' AND slot_key_hash=:slot_hash"
                    ),
                    {"user_id": user_id, "slot_hash": self._hash(slot_key)},
                )
            current = (
                (
                    await connection.execute(
                        text(
                            "SELECT revision,source_type FROM memory_control_records "
                            "WHERE user_id=:user_id AND store_key=:store_key FOR UPDATE"
                        ),
                        {"user_id": user_id, "store_key": store_key},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is not None and current["source_type"] == "EXPLICIT" and not user_requested:
                return None
            revision = int(current["revision"]) + 1 if current is not None else 1
            await connection.execute(
                text(
                    "INSERT INTO memory_control_records(id,user_id,store_key,slot_key_hash,"
                    "statement_hash,category,status,revision,source_type,policy_generation,expires_at,"
                    "source_conversation_id) VALUES(:id,:user_id,:store_key,:slot_hash,:statement_hash,"
                    ":category,'PENDING',:revision,:source_type,:generation,:expires_at,:conversation) "
                    "ON CONFLICT(user_id,store_key) DO UPDATE SET statement_hash=EXCLUDED.statement_hash,"
                    "category=EXCLUDED.category,status='PENDING',revision=EXCLUDED.revision,"
                    "source_type=EXCLUDED.source_type,policy_generation=EXCLUDED.policy_generation,"
                    "expires_at=EXCLUDED.expires_at,source_conversation_id=EXCLUDED.source_conversation_id,"
                    "store_deleted_at=NULL,updated_at=now()"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "store_key": store_key,
                    "slot_hash": self._hash(slot_key),
                    "statement_hash": self._hash(statement),
                    "category": category,
                    "revision": revision,
                    "source_type": "EXPLICIT" if user_requested else "AUTOMATIC",
                    "generation": generation,
                    "expires_at": expires_at,
                    "conversation": source_conversation_id,
                },
            )
        return PendingMemoryWrite(store_key, slot_key, revision, generation, expires_at)

    async def _activate(self, user_id: UUID, pending: PendingMemoryWrite) -> bool:
        async with self.database.transaction() as connection:
            result = await connection.execute(
                text(
                    "UPDATE memory_control_records m SET status='ACTIVE',updated_at=now() "
                    "FROM memory_policy_state p WHERE m.user_id=:user_id AND m.store_key=:store_key "
                    "AND m.revision=:revision AND m.status='PENDING' AND p.user_id=m.user_id "
                    "AND p.policy_generation=:generation AND m.policy_generation=:generation"
                ),
                {
                    "user_id": user_id,
                    "store_key": pending.store_key,
                    "revision": pending.revision,
                    "generation": pending.policy_generation,
                },
            )
            if result.rowcount:
                await connection.execute(
                    text(
                        "UPDATE memory_policy_state SET profile_snapshot_status='STALE',"
                        "updated_at=now() WHERE user_id=:user_id"
                    ),
                    {"user_id": user_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO user_profile_snapshots(user_id,status,policy_generation) "
                        "VALUES(:user_id,'STALE',:generation) ON CONFLICT(user_id) DO UPDATE SET "
                        "status='STALE',policy_generation=EXCLUDED.policy_generation,updated_at=now()"
                    ),
                    {"user_id": user_id, "generation": pending.policy_generation},
                )
        return bool(result.rowcount)

    async def _resolve_forget_target(self, user_id: UUID, statement: str) -> tuple[str, str]:
        category = self._category(statement)
        exact_slot = self._slot_key(statement, category)
        exact_key = self._hash(exact_slot)
        async with self.database.connect() as connection:
            exact = (
                await connection.execute(
                    text(
                        "SELECT 1 FROM memory_control_records WHERE user_id=:user_id "
                        "AND store_key=:store_key AND status IN ('PENDING','ACTIVE','CONFLICTED')"
                    ),
                    {"user_id": user_id, "store_key": exact_key},
                )
            ).scalar_one_or_none()
        if exact:
            return exact_key, exact_slot
        try:
            async with self._store() as store:
                items = await store.asearch(
                    self._namespace(user_id), query=statement, limit=5, refresh_ttl=False
                )
        except Exception as exc:
            raise ButlerError("MEMORY_STORE_UNAVAILABLE", "长期记忆暂时不可用", 503, True) from exc
        keys = [str(item.key) for item in items]
        if not keys:
            return exact_key, exact_slot
        async with self.database.connect() as connection:
            active = set(
                str(value)
                for value in (
                    await connection.execute(
                        text(
                            "SELECT store_key FROM memory_control_records WHERE user_id=:user_id "
                            "AND store_key=ANY(:keys) AND status='ACTIVE'"
                        ),
                        {"user_id": user_id, "keys": keys},
                    )
                ).scalars()
            )
        candidates = [item for item in items if str(item.key) in active]
        if len(candidates) > 1:
            raise ButlerError("MEMORY_TARGET_AMBIGUOUS", "要忘记的记忆不唯一，请描述得更具体", 409)
        if not candidates:
            return exact_key, exact_slot
        value_map = candidates[0].value if isinstance(candidates[0].value, dict) else {}
        return str(candidates[0].key), str(value_map.get("slot_key") or exact_slot)

    async def _set_automatic_enabled(self, user_id: UUID, enabled: bool) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO memory_policy_state(user_id,automatic_enabled) "
                    "VALUES(:user_id,:enabled) ON CONFLICT(user_id) DO UPDATE SET "
                    "automatic_enabled=EXCLUDED.automatic_enabled,"
                    "policy_generation=memory_policy_state.policy_generation+1,updated_at=now()"
                ),
                {"user_id": user_id, "enabled": enabled},
            )

    async def _next_generation(
        self, connection: Any, user_id: UUID, *, forget_all: bool = False
    ) -> int:
        row = (
            (
                await connection.execute(
                    text(
                        "UPDATE memory_policy_state SET policy_generation=policy_generation+1,"
                        "forget_before=CASE WHEN :forget_all THEN now() ELSE forget_before END,"
                        "profile_snapshot_status='STALE',updated_at=now() WHERE user_id=:user_id "
                        "RETURNING policy_generation"
                    ),
                    {"user_id": user_id, "forget_all": forget_all},
                )
            )
            .scalars()
            .one()
        )
        return int(row)

    async def _lock_user(self, connection: Any, user_id: UUID) -> None:
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:user_id,0))"),
            {"user_id": str(user_id)},
        )
        await connection.execute(
            text(
                "INSERT INTO memory_policy_state(user_id) VALUES(:user_id) "
                "ON CONFLICT(user_id) DO NOTHING"
            ),
            {"user_id": user_id},
        )

    async def _invalidate_pending(
        self, user_id: UUID, pending: PendingMemoryWrite, reason: str
    ) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE memory_control_records SET status='DELETED',updated_at=now() "
                    "WHERE user_id=:user_id AND store_key=:store_key AND revision=:revision "
                    "AND status='PENDING'"
                ),
                {
                    "user_id": user_id,
                    "store_key": pending.store_key,
                    "revision": pending.revision,
                },
            )
        await self._audit(user_id, reason, pending.store_key)

    async def _safe_store_delete(self, user_id: UUID, store_key: str) -> bool:
        try:
            async with self._store() as store:
                await store.adelete(self._namespace(user_id), store_key)
        except Exception:
            return False
        return True

    async def _mark_store_deleted(self, user_id: UUID, store_key: str) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE memory_control_records SET store_deleted_at=now(),updated_at=now() "
                    "WHERE user_id=:user_id AND store_key=:store_key AND status<>'ACTIVE'"
                ),
                {"user_id": user_id, "store_key": store_key},
            )

    async def _audit(self, user_id: UUID, action: str, key: str) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO memory_audit_records(id,user_id,action,memory_key_hash,metadata) "
                    "VALUES(:id,:user_id,:action,:key,CAST(:metadata AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "action": action,
                    "key": key,
                    "metadata": _json({"schema_version": "2.0"}),
                },
            )

    def _store(self) -> Any:
        return AsyncPostgresStore.from_conn_string(
            self._database_url,
            index=self._index,
            ttl=STORE_TTL_CONFIG,
        )

    @staticmethod
    def _namespace(user_id: UUID) -> tuple[str, ...]:
        return ("users", str(user_id), "long_term_memory")
