"""Scheduler 的记忆、画像和 Checkpoint 治理作业。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import text

from ..shared import _json, _row


class SchedulerMemoryJobsMixin:
    database: Any
    settings: Any
    memory: Any

    async def _extract_one_memory(self) -> bool:
        async with self.database.transaction() as connection:
            job = _row(
                await connection.execute(
                    text(
                        "SELECT j.*,m.content FROM memory_extraction_jobs j "
                        "JOIN messages m ON m.id=j.message_id "
                        "WHERE (j.status IN ('PENDING','RETRY') OR "
                        "(j.status='RUNNING' AND j.lease_expires_at<now())) "
                        "AND COALESCE(j.next_attempt_at,j.created_at)<=now() ORDER BY j.created_at "
                        "FOR UPDATE OF j SKIP LOCKED LIMIT 1"
                    )
                )
            )
            if job is None:
                return False
            await connection.execute(
                text(
                    "UPDATE memory_extraction_jobs SET status='RUNNING',attempt_count=attempt_count+1,"
                    "lease_expires_at=now()+interval '5 minutes',updated_at=now() WHERE id=:id"
                ),
                {"id": job["id"]},
            )
        try:
            await self.memory.extract_automatic(
                UUID(str(job["user_id"])),
                str(job["content"]),
                source_conversation_id=(
                    UUID(str(job["source_conversation_id"]))
                    if job["source_conversation_id"] is not None
                    else None
                ),
                expected_generation=int(job["policy_generation"]),
            )
        except Exception:
            async with self.database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE memory_extraction_jobs SET status=CASE WHEN attempt_count>=3 THEN 'DEAD' "
                        "ELSE 'RETRY' END,next_attempt_at=now()+interval '5 minutes',"
                        "error_code='MEMORY_EXTRACTION_FAILED',updated_at=now() WHERE id=:id"
                    ),
                    {"id": job["id"]},
                )
        else:
            async with self.database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE memory_extraction_jobs SET status='SUCCEEDED',lease_expires_at=NULL,"
                        "updated_at=now() WHERE id=:id"
                    ),
                    {"id": job["id"]},
                )
        return True

    async def _cleanup_one_memory_record(self) -> bool:
        async with self.database.transaction() as connection:
            record = _row(
                await connection.execute(
                    text(
                        "SELECT user_id,store_key,status FROM memory_control_records "
                        "WHERE store_deleted_at IS NULL AND ((status='PENDING' "
                        "AND updated_at<now()-interval '1 hour') "
                        "OR status IN ('DELETED','EXPIRED') OR "
                        "(status='ACTIVE' AND expires_at IS NOT NULL AND expires_at<=now())) "
                        "ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1"
                    )
                )
            )
            if record is not None:
                await connection.execute(
                    text(
                        "UPDATE memory_control_records SET status=CASE WHEN status='ACTIVE' "
                        "THEN 'EXPIRED' ELSE status END,updated_at=now() "
                        "WHERE user_id=:user_id AND store_key=:store_key"
                    ),
                    {"user_id": record["user_id"], "store_key": record["store_key"]},
                )
        if record is not None:
            deleted = await self.memory._safe_store_delete(
                UUID(str(record["user_id"])), str(record["store_key"])
            )
            if deleted:
                await self.memory._mark_store_deleted(
                    UUID(str(record["user_id"])), str(record["store_key"])
                )
            return True
        async with self.memory._store() as store:
            return bool(await store.sweep_ttl())

    async def _refresh_one_profile_snapshot(self) -> bool:
        async with self.database.connect() as connection:
            policy = _row(
                await connection.execute(
                    text(
                        "SELECT user_id,policy_generation FROM memory_policy_state "
                        "WHERE profile_snapshot_status='STALE' ORDER BY updated_at LIMIT 1"
                    )
                )
            )
        if policy is None:
            return False
        user_id = UUID(str(policy["user_id"]))
        generation = int(policy["policy_generation"])
        namespace = ("users", str(user_id), "long_term_memory")
        async with self.memory._store() as store:
            items = await store.asearch(namespace, limit=100, refresh_ttl=False)
        keys = [str(item.key) for item in items]
        active: dict[str, dict[str, object]] = {}
        if keys:
            async with self.database.connect() as connection:
                active = {
                    str(row["store_key"]): dict(row)
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT store_key,revision,category,policy_generation "
                                "FROM memory_control_records WHERE user_id=:user_id "
                                "AND store_key=ANY(:keys) AND status='ACTIVE' "
                                "AND (expires_at IS NULL OR expires_at>now())"
                            ),
                            {"user_id": user_id, "keys": keys},
                        )
                    ).mappings()
                }
        grouped: dict[str, list[str]] = {
            "preferences": [],
            "habits": [],
            "constraints": [],
            "background": [],
        }
        category_key = {
            "PREFERENCE": "preferences",
            "HABIT": "habits",
            "CONSTRAINT": "constraints",
            "BACKGROUND": "background",
        }
        for item in items:
            control = active.get(str(item.key))
            value = item.value if isinstance(item.value, dict) else {}
            if (
                control is None
                or int(value.get("revision") or 0) != int(str(control["revision"]))
                or int(value.get("policy_generation") or 0)
                != int(str(control["policy_generation"]))
            ):
                continue
            statement = str(value.get("statement") or "").strip()
            if statement:
                grouped[category_key[str(control["category"])]].append(statement)
        async with self.database.transaction() as connection:
            current_generation = (
                await connection.execute(
                    text(
                        "SELECT policy_generation FROM memory_policy_state "
                        "WHERE user_id=:user_id FOR UPDATE"
                    ),
                    {"user_id": user_id},
                )
            ).scalar_one()
            if int(current_generation) != generation:
                return True
            await connection.execute(
                text(
                    "INSERT INTO user_profile_snapshots(user_id,status,policy_generation,profile_data) "
                    "VALUES(:user_id,'FRESH',:generation,CAST(:profile AS jsonb)) "
                    "ON CONFLICT(user_id) DO UPDATE SET status='FRESH',"
                    "policy_generation=EXCLUDED.policy_generation,profile_data=EXCLUDED.profile_data,"
                    "updated_at=now()"
                ),
                {
                    "user_id": user_id,
                    "generation": generation,
                    "profile": _json({"schema_version": "1.0", **grouped}),
                },
            )
            await connection.execute(
                text(
                    "UPDATE memory_policy_state SET profile_snapshot_status='FRESH',updated_at=now() "
                    "WHERE user_id=:user_id AND policy_generation=:generation"
                ),
                {"user_id": user_id, "generation": generation},
            )
        return True

    async def _cleanup_one_store_orphan(self) -> bool:
        async with self.database.connect() as connection:
            user_id_value = (
                await connection.execute(
                    text(
                        "SELECT user_id FROM memory_policy_state ORDER BY updated_at,user_id LIMIT 1"
                    )
                )
            ).scalar_one_or_none()
        if user_id_value is None:
            return False
        user_id = UUID(str(user_id_value))
        namespace = ("users", str(user_id), "long_term_memory")
        offset = 0
        async with self.memory._store() as store:
            while True:
                items = await store.asearch(namespace, limit=100, offset=offset, refresh_ttl=False)
                if not items:
                    break
                keys = [str(item.key) for item in items]
                async with self.database.connect() as connection:
                    rows = (
                        (
                            await connection.execute(
                                text(
                                    "SELECT store_key,status,revision,policy_generation FROM "
                                    "memory_control_records WHERE user_id=:user_id "
                                    "AND store_key=ANY(:keys) AND (status='ACTIVE' OR "
                                    "(status='PENDING' AND updated_at>=now()-interval '1 hour'))"
                                ),
                                {"user_id": user_id, "keys": keys},
                            )
                        )
                        .mappings()
                        .all()
                    )
                controls = {str(row["store_key"]): row for row in rows}
                for item in items:
                    control = controls.get(str(item.key))
                    value = item.value if isinstance(item.value, dict) else {}
                    valid = control is not None and (
                        control["status"] == "PENDING"
                        or (
                            int(value.get("revision") or 0) == int(control["revision"])
                            and int(value.get("policy_generation") or 0)
                            == int(control["policy_generation"])
                        )
                    )
                    if not valid:
                        await store.adelete(namespace, str(item.key))
                        return True
                offset += len(items)
        async with self.database.transaction() as connection:
            await connection.execute(
                text("UPDATE memory_policy_state SET updated_at=now() WHERE user_id=:user_id"),
                {"user_id": user_id},
            )
        return False

    async def _delete_one_expired_checkpoint(self) -> bool:
        async with self.database.connect() as connection:
            segment = _row(
                await connection.execute(
                    text(
                        "SELECT s.id,s.thread_id FROM conversation_segments s "
                        "JOIN conversations c ON c.id=s.conversation_id "
                        "WHERE s.checkpoint_deleted_at IS NULL AND "
                        "(s.checkpoint_delete_requested_at IS NOT NULL OR "
                        "(s.status='ARCHIVED' AND s.archived_at<now()-(:days || ' days')::interval) "
                        "OR (c.status='ARCHIVED' AND c.archived_at<now()-(:days || ' days')::interval)) "
                        "ORDER BY COALESCE(s.checkpoint_delete_requested_at,s.archived_at,c.archived_at) "
                        "LIMIT 1"
                    ),
                    {"days": self.settings.checkpoint_retention_days},
                )
            )
        if segment is None:
            return False
        async with AsyncPostgresSaver.from_conn_string(
            self.settings.langgraph_database_url
        ) as saver:
            await saver.adelete_thread(str(segment["thread_id"]))
        async with self.database.transaction() as connection:
            await connection.execute(
                text("UPDATE conversation_segments SET checkpoint_deleted_at=now() WHERE id=:id"),
                {"id": segment["id"]},
            )
        return True
