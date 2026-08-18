from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID, uuid5

from sqlalchemy import text

from ai_butler.adapters.documents import SUPPORTED_RAG_MIME_TYPES, chunk_text, extract_text
from ai_butler.adapters.embedding import EmbeddingProviderError
from ai_butler.adapters.vector import VectorPoint, VectorStoreError

from .context import ButlerContext
from .shared import (
    _row,
)


class SchedulerService:
    def __init__(self, context: ButlerContext) -> None:
        self.database = context.database
        self.settings = context.settings
        self.embedding_provider = context.embedding_provider
        self.vector_store = context.vector_store

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
                        "UPDATE users SET nickname=NULL,phone_ciphertext=NULL,status='DELETED',"
                        "deleted_at=now(),updated_at=now() WHERE id=:id"
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
                if len(vector) != self.embedding_provider.dimensions:
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
