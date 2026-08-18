from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID, uuid5

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from sqlalchemy import text

from ai_butler.adapters.documents import SUPPORTED_RAG_MIME_TYPES, chunk_text, extract_text
from ai_butler.adapters.embedding import EmbeddingProviderError
from ai_butler.adapters.notification import Notification
from ai_butler.adapters.vector import VectorPoint, VectorStoreError
from ai_butler.agent.evidence import estimate_tokens

from .context import ButlerContext
from .memory import LongTermMemoryService
from .retention import RetentionService
from .shared import (
    _row,
)


class SchedulerService:
    def __init__(self, context: ButlerContext) -> None:
        self.database = context.database
        self.settings = context.settings
        self.embedding_provider = context.embedding_provider
        self.vector_store = context.vector_store
        self.notification_provider = context.notification_provider
        self.memory = LongTermMemoryService(context)
        self.retention = RetentionService(context)

    async def scheduler_poll_once(self) -> bool:
        """处理一个可安全重试的知识、记忆、通知或治理作业。"""

        if await self._delete_one_knowledge_vector():
            return True
        if await self._ingest_one_private_file():
            return True
        if await self._extract_one_memory():
            return True
        if await self._send_one_notification():
            return True
        if await self._run_one_account_deletion_step():
            return True
        return await self.retention.cleanup_once()

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
            await self.memory.extract_automatic(UUID(str(job["user_id"])), str(job["content"]))
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

    async def _send_one_notification(self) -> bool:
        async with self.database.transaction() as connection:
            notification = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM notification_jobs WHERE (status IN ('PENDING','RETRY') OR "
                        "(status='RUNNING' AND updated_at<now()-interval '10 minutes')) "
                        "AND COALESCE(next_attempt_at,scheduled_at)<=now() ORDER BY scheduled_at "
                        "FOR UPDATE SKIP LOCKED LIMIT 1"
                    )
                )
            )
            if notification is None:
                return False
            await connection.execute(
                text(
                    "UPDATE notification_jobs SET status='RUNNING',attempt_count=attempt_count+1,"
                    "updated_at=now() WHERE id=:id"
                ),
                {"id": notification["id"]},
            )
        try:
            provider_id = await self.notification_provider.send(
                Notification(
                    user_id=UUID(str(notification["user_id"])),
                    template=str(notification["event_type"]),
                    idempotency_key=str(notification["idempotency_key"]),
                )
            )
        except Exception:
            attempt = int(notification["attempt_count"]) + 1
            delays = (1, 5, 30)
            next_delay = delays[min(attempt - 1, len(delays) - 1)]
            async with self.database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE notification_jobs SET status=CASE WHEN attempt_count>=max_attempts "
                        "THEN 'DEAD' ELSE 'RETRY' END,next_attempt_at=CASE WHEN attempt_count>=max_attempts "
                        "THEN NULL ELSE now()+(:delay || ' minutes')::interval END,"
                        "last_error_code='NOTIFICATION_PROVIDER_FAILED',updated_at=now() WHERE id=:id"
                    ),
                    {"id": notification["id"], "delay": next_delay},
                )
        else:
            async with self.database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE notification_jobs SET status='SENT',provider_message_id=:provider_id,"
                        "sent_at=now(),last_error_code=NULL,updated_at=now() WHERE id=:id"
                    ),
                    {"id": notification["id"], "provider_id": provider_id},
                )
        return True

    async def _run_one_account_deletion_step(self) -> bool:
        async with self.database.transaction() as connection:
            job = _row(
                await connection.execute(
                    text(
                        "SELECT j.* FROM account_deletion_jobs j JOIN users u ON u.id=j.user_id "
                        "WHERE (j.status IN ('PENDING','RETRY') OR "
                        "(j.status='RUNNING' AND j.lease_expires_at<now())) AND u.status='DELETING' "
                        "AND COALESCE(j.next_attempt_at,j.created_at)<=now() ORDER BY j.created_at "
                        "FOR UPDATE OF j SKIP LOCKED LIMIT 1"
                    )
                )
            )
            if job is None:
                return False
            await connection.execute(
                text(
                    "UPDATE account_deletion_jobs SET status='RUNNING',attempt_count=attempt_count+1,"
                    "lease_expires_at=now()+interval '5 minutes',updated_at=now() WHERE id=:id"
                ),
                {"id": job["id"]},
            )
        try:
            await self._execute_deletion_step(job)
        except Exception:
            async with self.database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE account_deletion_jobs SET status=CASE WHEN attempt_count>=10 THEN 'DEAD' "
                        "ELSE 'RETRY' END,next_attempt_at=now()+interval '5 minutes',"
                        "error_code='ACCOUNT_DELETION_STEP_FAILED',updated_at=now() WHERE id=:id"
                    ),
                    {"id": job["id"]},
                )
        return True

    async def _execute_deletion_step(self, job: dict[str, object]) -> None:
        user_id = UUID(str(job["user_id"]))
        step = str(job["current_step"])
        if step == "CANCEL_WORK":
            async with self.database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE agent_runs SET status='CANCELLED',cancel_requested_at=now(),updated_at=now() "
                        "WHERE user_id=:user_id AND status NOT IN ('SUCCEEDED','FAILED_FINAL','CANCELLED')"
                    ),
                    {"user_id": user_id},
                )
                await connection.execute(
                    text(
                        "UPDATE notification_jobs SET status='CANCELLED',updated_at=now() "
                        "WHERE user_id=:user_id AND status IN ('PENDING','RUNNING','RETRY')"
                    ),
                    {"user_id": user_id},
                )
            await self._advance_deletion(job, "CHECKPOINT")
            return
        if step == "CHECKPOINT":
            async with self.database.connect() as connection:
                thread_ids = (
                    (
                        await connection.execute(
                            text(
                                "SELECT s.thread_id FROM conversation_segments s "
                                "JOIN conversations c ON c.id=s.conversation_id WHERE c.user_id=:user_id"
                            ),
                            {"user_id": user_id},
                        )
                    )
                    .scalars()
                    .all()
                )
            async with AsyncPostgresSaver.from_conn_string(
                self.settings.langgraph_database_url
            ) as saver:
                for thread_id in thread_ids:
                    await saver.adelete_thread(str(thread_id))
            await self._advance_deletion(job, "STORE")
            return
        if step == "STORE":
            namespace = ("users", str(user_id), "long_term_memory")
            async with AsyncPostgresStore.from_conn_string(
                self.settings.langgraph_database_url,
                ttl={"refresh_on_read": False, "sweep_interval_minutes": 60},
            ) as store:
                memories = await store.asearch(namespace, limit=1000, refresh_ttl=False)
                for memory in memories:
                    await store.adelete(namespace, str(memory.key))
            await self._advance_deletion(job, "QDRANT")
            return
        if step == "QDRANT":
            async with self.database.connect() as connection:
                remaining = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM knowledge_documents WHERE owner_user_id=:user_id"
                        ),
                        {"user_id": user_id},
                    )
                ).scalar_one()
            if int(remaining) == 0:
                await self._advance_deletion(job, "OBJECTS")
            return
        if step == "OBJECTS":
            async with self.database.connect() as connection:
                object_keys = (
                    (
                        await connection.execute(
                            text("SELECT object_key FROM stored_files WHERE user_id=:user_id"),
                            {"user_id": user_id},
                        )
                    )
                    .scalars()
                    .all()
                )
            root = self.settings.object_storage_local_path.resolve()
            for object_key in object_keys:
                path = (root / str(object_key)).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("stored object key escapes configured storage root")
                if path.is_file():
                    await asyncio.to_thread(path.unlink)
            await self._advance_deletion(job, "BUSINESS")
            return
        if step == "BUSINESS":
            await self._delete_business_data(job, user_id)

    async def _advance_deletion(self, job: dict[str, object], next_step: str) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE account_deletion_jobs SET status='PENDING',current_step=:step,"
                    "next_attempt_at=NULL,lease_expires_at=NULL,error_code=NULL,updated_at=now() WHERE id=:id"
                ),
                {"id": job["id"], "step": next_step},
            )

    async def _delete_business_data(self, job: dict[str, object], user_id: UUID) -> None:
        async with self.database.transaction() as connection:
            phone_hash = (
                await connection.execute(
                    text("SELECT phone_hash FROM users WHERE id=:user_id FOR UPDATE"),
                    {"user_id": user_id},
                )
            ).scalar_one_or_none()
            if phone_hash is not None:
                await connection.execute(
                    text("DELETE FROM phone_verification_challenges WHERE phone_hash=:phone_hash"),
                    {"phone_hash": phone_hash},
                )
            for table in (
                "notification_jobs",
                "goals",
                "conversations",
                "user_agents",
                "study_availability",
                "user_profiles",
                "stored_files",
                "memory_policy_state",
                "memory_tombstones",
                "memory_audit_records",
                "auth_sessions",
                "user_identities",
            ):
                await connection.execute(
                    text(f"DELETE FROM {table} WHERE user_id=:user_id"),  # noqa: S608
                    {"user_id": user_id},
                )
            await connection.execute(
                text(
                    "UPDATE users SET nickname=NULL,phone_ciphertext=NULL,phone_hash=NULL,"
                    "locale='zh-CN',timezone='Asia/Shanghai',status='DELETED',"
                    "deleted_at=now(),updated_at=now() WHERE id=:user_id"
                ),
                {"user_id": user_id},
            )
            await connection.execute(
                text(
                    "UPDATE account_deletion_jobs SET status='SUCCEEDED',current_step='DONE',"
                    "lease_expires_at=NULL,error_code=NULL,updated_at=now() WHERE id=:id"
                ),
                {"id": job["id"]},
            )

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
                        "SELECT sf.* FROM stored_files sf JOIN users u ON u.id=sf.user_id "
                        "LEFT JOIN knowledge_documents kd ON kd.stored_file_id=sf.id "
                        "WHERE u.status='ACTIVE' AND sf.upload_status='VERIFIED' "
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
            if len(chunks) > self.settings.rag_max_chunks_per_document:
                raise ValueError("document exceeds configured chunk limit")
            points: list[VectorPoint] = []
            rows: list[dict[str, object]] = []
            embedding_batch_size = self.settings.rag_embedding_batch_size
            for offset in range(0, len(chunks), embedding_batch_size):
                batch = chunks[offset : offset + embedding_batch_size]
                vectors = await self.embedding_provider.embed_many(
                    tuple(chunk.content for chunk in batch)
                )
                if len(vectors) != len(batch):
                    raise ValueError("embedding batch mismatch")
                for chunk, vector in zip(batch, vectors, strict=True):
                    if len(vector) != self.embedding_provider.dimensions:
                        raise ValueError("embedding dimension mismatch")
                    digest = hashlib.sha256(chunk.content.encode()).hexdigest()
                    chunk_id = uuid5(document_id, f"chunk:{chunk.index}:{digest}")
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
                            "heading": chunk.heading_path,
                            "content": chunk.content,
                            "tokens": estimate_tokens(chunk.content),
                            "hash": digest,
                        }
                    )
            vector_batch_size = self.settings.rag_vector_upsert_batch_size
            for offset in range(0, len(points), vector_batch_size):
                await self.vector_store.upsert(tuple(points[offset : offset + vector_batch_size]))
            async with self.database.transaction() as connection:
                await connection.execute(
                    text("DELETE FROM knowledge_chunks WHERE document_id=:document"),
                    {"document": document_id},
                )
                for row in rows:
                    await connection.execute(
                        text(
                            "INSERT INTO knowledge_chunks(id,document_id,chunk_index,heading_path,content,token_count,"
                            "content_hash,embedding_model,qdrant_collection,qdrant_point_id,vector_status) "
                            "VALUES(:id,:document,:index,:heading,:content,:tokens,:hash,:model,:collection,:id,'READY')"
                        ),
                        {
                            **row,
                            "document": document_id,
                            "model": self.embedding_provider.model,
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
        except (OSError, UnicodeError, ValueError, EmbeddingProviderError, VectorStoreError):
            async with self.database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE knowledge_documents SET ingestion_status='FAILED',"
                        "error_code='KNOWLEDGE_INGESTION_FAILED',updated_at=now() WHERE id=:id"
                    ),
                    {"id": document_id},
                )
        return True
